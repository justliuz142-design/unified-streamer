"""In-memory download queue with bounded concurrency and websocket emission.

This module owns the lifecycle of every download:

* Validates incoming requests.
* Streams progress events to all connected Socket.IO clients.
* Handles cancellation cleanly.
* Records every job into a rolling history file for the frontend.
* Survives individual download failures — one bad job never blocks the queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import ytdl_engine
from config import settings

log = logging.getLogger("queue")

_HISTORY_FILE = settings.state_dir / "history.json"
_HISTORY_LIMIT = 200


@dataclass
class DownloadJob:
    id: str
    url: str
    title: str
    download_type: str
    status: str = "pending"  # pending|downloading|finished|error|cancelled
    progress: float = 0.0
    speed: Optional[float] = None
    eta: Optional[int] = None
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    download_url: Optional[str] = None  # served path for the user

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DownloadQueue:
    def __init__(self, emit_async, max_concurrent: int = 3) -> None:
        self._emit_async = emit_async  # async callable: emit(event, payload)
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, DownloadJob] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---- public API --------------------------------------------------------

    async def enqueue(self, payload: dict[str, Any]) -> DownloadJob:
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")

        # Eagerly fetch metadata for a nice title in the queue UI. Failure here
        # is non-fatal — the download itself may still recover via fallbacks.
        title = url
        try:
            info = await ytdl_engine.extract_metadata(url, flat_playlist=True)
            title = info.title
        except Exception as exc:
            log.warning("Pre-fetch metadata failed for %s: %s", url, exc)

        job = DownloadJob(
            id=uuid.uuid4().hex,
            url=url,
            title=title,
            download_type=payload.get("download_type") or "video",
        )
        async with self._lock:
            self._jobs[job.id] = job
        await self._emit("added", job.to_dict())
        task = asyncio.create_task(self._run(job, payload))
        self._tasks[job.id] = task
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        event = self._cancel_events.get(job_id)
        if event:
            event.set()
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        if job.status in {"pending", "downloading"}:
            job.status = "cancelled"
            job.finished_at = time.time()
            await self._emit("cancelled", job.to_dict())
        return True

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    async def get(self, job_id: str) -> Optional[dict[str, Any]]:
        job = self._jobs.get(job_id)
        return job.to_dict() if job else None

    async def clear_finished(self) -> int:
        async with self._lock:
            to_remove = [j.id for j in self._jobs.values() if j.status in {"finished", "error", "cancelled"}]
            for jid in to_remove:
                self._jobs.pop(jid, None)
                self._cancel_events.pop(jid, None)
                self._tasks.pop(jid, None)
        return len(to_remove)

    # ---- history -----------------------------------------------------------

    def load_history(self) -> list[dict[str, Any]]:
        if not _HISTORY_FILE.exists():
            return []
        try:
            return json.loads(_HISTORY_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _append_history(self, job: DownloadJob) -> None:
        history = self.load_history()
        history.insert(0, job.to_dict())
        history = history[:_HISTORY_LIMIT]
        try:
            _HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Cannot persist history: %s", exc)

    def clear_history(self) -> None:
        try:
            _HISTORY_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- internals ---------------------------------------------------------

    async def _emit(self, event: str, payload: Any) -> None:
        try:
            await self._emit_async(event, payload)
        except Exception:
            log.exception("Failed to emit %s", event)

    def _make_progress_hook(self, job: DownloadJob):
        """Build a yt-dlp progress hook that bridges into the asyncio loop."""
        loop = self._loop
        last_emit = [0.0]

        def hook(d: dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = int(d.get("downloaded_bytes") or 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                job.downloaded_bytes = downloaded
                job.total_bytes = int(total) if total else job.total_bytes
                job.speed = float(d.get("speed")) if d.get("speed") else None
                job.eta = int(d.get("eta")) if d.get("eta") is not None else None
                job.status = "downloading"
                if job.total_bytes:
                    job.progress = max(0.0, min(100.0, downloaded * 100.0 / job.total_bytes))
                job.filename = d.get("filename") or job.filename
                now = time.monotonic()
                # Throttle to ~5 updates/sec per job to keep the websocket light.
                if now - last_emit[0] >= 0.2 or job.progress >= 100:
                    last_emit[0] = now
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._emit("progress", job.to_dict()), loop
                        )
            elif status == "finished":
                job.progress = 100.0
                job.filename = d.get("filename") or job.filename
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._emit("progress", job.to_dict()), loop
                    )
            elif status == "error":
                job.status = "error"
                job.error = str(d.get("error") or "Download error")

        return hook

    async def _run(self, job: DownloadJob, payload: dict[str, Any]) -> None:
        async with self._semaphore:
            cancel_event = threading.Event()
            self._cancel_events[job.id] = cancel_event
            try:
                job.status = "downloading"
                await self._emit("progress", job.to_dict())

                outdir = settings.download_dir / job.id
                outdir.mkdir(parents=True, exist_ok=True)
                output_template = str(outdir / "%(title)s.%(ext)s")
                hook = self._make_progress_hook(job)
                loop = asyncio.get_running_loop()

                info = await loop.run_in_executor(
                    ytdl_engine.get_executor(),
                    lambda: ytdl_engine.submit_download(
                        url=job.url,
                        output_template=output_template,
                        download_type=payload.get("download_type") or "video",
                        quality=payload.get("quality"),
                        container=payload.get("container"),
                        audio_format=payload.get("audio_format"),
                        audio_quality=payload.get("audio_quality"),
                        subtitle_langs=payload.get("subtitle_langs"),
                        write_subs=bool(payload.get("write_subs")),
                        write_auto_subs=bool(payload.get("write_auto_subs")),
                        embed_subs=bool(payload.get("embed_subs")),
                        embed_thumb=bool(payload.get("embed_thumb")),
                        embed_meta=bool(payload.get("embed_meta", True)),
                        embed_chapters=bool(payload.get("embed_chapters")),
                        playlist_start=payload.get("playlist_start"),
                        playlist_end=payload.get("playlist_end"),
                        progress_cb=hook,
                        cancel_event=cancel_event,
                    ),
                )
                # Resolve the final filename produced by yt-dlp.
                final_path = self._resolve_final_path(outdir, info)
                if final_path:
                    job.filename = str(final_path.name)
                    job.download_url = f"/files/{job.id}/{quote(final_path.name)}"
                job.status = "finished"
                job.progress = 100.0
                job.finished_at = time.time()
                await self._emit("completed", job.to_dict())
                self._append_history(job)
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.error = "Cancelled"
                job.finished_at = time.time()
                await self._emit("cancelled", job.to_dict())
            except Exception as exc:
                log.exception("Download failed for job %s", job.id)
                job.status = "error"
                job.error = str(exc)
                job.finished_at = time.time()
                await self._emit("error", job.to_dict())
                self._append_history(job)
            finally:
                self._cancel_events.pop(job.id, None)

    @staticmethod
    def _resolve_final_path(outdir: Path, info: dict[str, Any]) -> Optional[Path]:
        # yt-dlp records the actual on-disk filename here after post-processing.
        candidates: list[Path] = []
        if rq := info.get("requested_downloads"):
            for r in rq:
                if r.get("filepath"):
                    candidates.append(Path(r["filepath"]))
        if not candidates and info.get("filepath"):
            candidates.append(Path(info["filepath"]))
        if candidates:
            return candidates[0]
        # Fall back to whatever file exists in the dir (largest by size).
        files = [p for p in outdir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_size)
