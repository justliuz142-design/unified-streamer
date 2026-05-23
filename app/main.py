"""Aiohttp + Socket.IO entry point.

Routes (intentionally short and Metube-style):

* ``GET  /metadata?url=...``  — extract media metadata (Python yt-dlp API)
* ``POST /download``          — enqueue a download
* ``GET  /progress``          — snapshot of the current queue + per-job state
* ``GET  /queue``             — alias for ``/progress`` (kept for clarity)
* ``POST /cancel``            — cancel a job by id
* ``GET  /history``           — list completed jobs
* ``DELETE /history``         — clear history
* ``POST /cleanup``           — force a cleanup sweep
* ``GET  /files/<job>/<name>``— stream a downloaded file to the user

Realtime: Socket.IO events ``added``, ``progress``, ``completed``,
``cancelled``, ``error`` are emitted to every connected client.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
import socketio

import ytdl_engine
from cleanup import CleanupScheduler
from config import settings
from queue_manager import DownloadQueue

log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# App + Socket.IO wiring
# ---------------------------------------------------------------------------

def _cors_origins() -> list[str] | str:
    raw = settings.cors_origins.strip()
    if not raw or raw == "*":
        return "*"
    return [o.strip() for o in raw.split(",") if o.strip()]


sio = socketio.AsyncServer(
    async_mode="aiohttp",
    cors_allowed_origins=_cors_origins(),
    ping_interval=20,
    ping_timeout=20,
)


async def _emit(event: str, payload):  # noqa: ANN001
    await sio.emit(event, payload)


queue = DownloadQueue(_emit, max_concurrent=settings.max_concurrent)
cleanup = CleanupScheduler(queue)


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------

routes = web.RouteTableDef()


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@routes.get("/metadata")
async def metadata(request: web.Request) -> web.Response:
    url = request.query.get("url", "").strip()
    if not url:
        return _json_error(400, "url query param is required")
    try:
        info = await ytdl_engine.extract_metadata(url, flat_playlist=False)
    except Exception as exc:
        log.warning("metadata error for %s: %s", url, exc)
        return _json_error(502, f"metadata failed: {exc}")
    return web.json_response(info.as_dict())


@routes.post("/download")
async def download(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return _json_error(400, "invalid JSON body")
    if not isinstance(payload, dict):
        return _json_error(400, "body must be a JSON object")
    try:
        job = await queue.enqueue(payload)
    except ValueError as exc:
        return _json_error(400, str(exc))
    except Exception as exc:
        log.exception("Failed to enqueue download")
        return _json_error(500, f"enqueue failed: {exc}")
    return web.json_response(job.to_dict(), status=202)


@routes.get("/progress")
@routes.get("/queue")
async def progress(_: web.Request) -> web.Response:
    return web.json_response(await queue.snapshot())


@routes.post("/cancel")
async def cancel(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    job_id = (payload or {}).get("id") or request.query.get("id")
    if not job_id:
        return _json_error(400, "id is required")
    ok = await queue.cancel(job_id)
    return web.json_response({"cancelled": ok})


@routes.get("/history")
async def history(_: web.Request) -> web.Response:
    return web.json_response(queue.load_history())


@routes.delete("/history")
async def history_clear(_: web.Request) -> web.Response:
    queue.clear_history()
    return web.json_response({"cleared": True})


@routes.post("/cleanup")
async def cleanup_now(_: web.Request) -> web.Response:
    n = await cleanup.sweep()
    return web.json_response({"removed": n})


@routes.get("/health")
async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "queue": len((await queue.snapshot()))})


@routes.get("/files/{job_id}/{name}")
async def serve_file(request: web.Request) -> web.StreamResponse:
    job_id = request.match_info["job_id"]
    name = request.match_info["name"]
    # Defend against traversal.
    if "/" in name or ".." in name or "\\" in name:
        return _json_error(400, "invalid filename")
    base = settings.download_dir / job_id
    target = (base / name).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return _json_error(400, "invalid path")
    if not target.exists() or not target.is_file():
        return _json_error(404, "file not found")
    content_type, _ = mimetypes.guess_type(target.name)
    # Use RFC 6266 encoded filename* parameter so special chars (# > < & spaces)
    # are preserved correctly in all browsers.
    encoded_name = quote(target.name, safe="")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{target.name}"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
        "Cache-Control": "no-store",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return web.FileResponse(target, headers=headers)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

# Look in ui/dist/metube first (the Dockerfile output), then fall back to
# ui/src so local ``python -m app.main`` development works without a build.
_STATIC_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "ui" / "dist" / "metube",
    settings.static_dir,
]


def _resolve_static_dir() -> Path:
    for c in _STATIC_CANDIDATES:
        if c.is_dir() and (c / "index.html").exists():
            return c
    # As a final fallback, the configured static dir even if missing — aiohttp
    # will raise a clear error.
    return settings.static_dir


@routes.get("/")
async def index(_: web.Request) -> web.Response:
    static = _resolve_static_dir()
    return web.FileResponse(static / "index.html")


# ---------------------------------------------------------------------------
# Socket.IO lifecycle
# ---------------------------------------------------------------------------

@sio.event
async def connect(sid, _environ):  # noqa: ANN001
    log.info("client connected: %s", sid)
    # Push current queue state so the new client renders the right thing.
    await sio.emit("snapshot", await queue.snapshot(), to=sid)


@sio.event
async def disconnect(sid):  # noqa: ANN001
    log.info("client disconnected: %s", sid)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.add_routes(routes)
    sio.attach(app, socketio_path="socket.io")

    static_dir = _resolve_static_dir()
    log.info("Serving static frontend from %s", static_dir)
    app.router.add_static("/", static_dir, show_index=False, follow_symlinks=False)

    async def _on_startup(_app: web.Application) -> None:
        queue.bind_loop(asyncio.get_running_loop())
        cleanup.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await cleanup.stop()
        ytdl_engine.shutdown()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    app = create_app()
    log.info("Starting LunarMediaDL on %s:%s", settings.host, settings.port)
    web.run_app(app, host=settings.host, port=settings.port, print=None)


if __name__ == "__main__":
    main()
