"""Python-native yt-dlp integration.

Strict rules enforced here:

* Never use ``subprocess``, ``os.system`` or any shell invocation of yt-dlp.
* Always go through ``yt_dlp.YoutubeDL`` directly.
* Use progress hooks for realtime stats (bytes / speed / eta / fragments).
* Apply consistent options for metadata extraction *and* download, so a video
  that resolves during ``extract_info`` is guaranteed to be downloadable with
  the same client + cookies + extractor args.
* Fall back across multiple YouTube clients (android → web → tv → ios) and
  across format selectors when the requested one is unavailable.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from config import settings

log = logging.getLogger("ytdl")

# Single shared executor for blocking yt-dlp calls. Each download still runs in
# its own thread, but pooling avoids unbounded thread creation under load.
_executor = ThreadPoolExecutor(max_workers=max(8, settings.max_concurrent * 2), thread_name_prefix="ytdl")

# Order matters: android tends to expose the widest range of formats without
# nsig challenges; web/tv/ios are kept as graceful fallbacks.
_YT_CLIENTS = ["android", "web", "tv", "ios"]


def _base_options() -> dict[str, Any]:
    """Options shared by metadata extraction and download.

    Keeping this single source of truth eliminates the classic MeTube bug
    where metadata succeeds but the download fails because the extractor used
    a different client configuration.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": False,
        "cachedir": str(settings.ytdl_cache_dir),
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 4,
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": _YT_CLIENTS,
                "skip": ["dash_manifest"] if False else [],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    cookies = settings.cookies_file
    if cookies is not None:
        opts["cookiefile"] = str(cookies)
    if settings.proxy_url:
        opts["proxy"] = settings.proxy_url
    return opts


@dataclass
class MediaInfo:
    """Minimal metadata shape consumed by the frontend."""

    id: str
    title: str
    uploader: Optional[str]
    duration: Optional[int]
    view_count: Optional[int]
    upload_date: Optional[str]
    thumbnail: Optional[str]
    description: Optional[str]
    webpage_url: str
    is_playlist: bool
    entries: list[dict[str, Any]]
    formats: list[dict[str, Any]]

    @classmethod
    def from_info(cls, info: dict[str, Any]) -> "MediaInfo":
        is_playlist = info.get("_type") == "playlist" or "entries" in info
        entries: list[dict[str, Any]] = []
        if is_playlist:
            for entry in (info.get("entries") or []):
                if not entry:
                    continue
                entries.append({
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                    "url": entry.get("webpage_url") or entry.get("url"),
                })
        formats: list[dict[str, Any]] = []
        for f in (info.get("formats") or []):
            if not f.get("format_id"):
                continue
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or (
                    f"{f.get('width')}x{f.get('height')}" if f.get("width") else None
                ),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "abr": f.get("abr"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "format_note": f.get("format_note"),
                "tbr": f.get("tbr"),
            })
        return cls(
            id=str(info.get("id") or ""),
            title=str(info.get("title") or "Unknown"),
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            view_count=info.get("view_count"),
            upload_date=info.get("upload_date"),
            thumbnail=info.get("thumbnail"),
            description=(info.get("description") or "")[:1000] or None,
            webpage_url=info.get("webpage_url") or info.get("original_url") or "",
            is_playlist=is_playlist,
            entries=entries,
            formats=formats,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

async def extract_metadata(url: str, *, flat_playlist: bool = True) -> MediaInfo:
    """Return media metadata for ``url`` using the Python yt-dlp API."""
    loop = asyncio.get_running_loop()

    def _run() -> MediaInfo:
        last_exc: Optional[Exception] = None
        for client in _YT_CLIENTS:
            opts = _base_options()
            opts["extractor_args"]["youtube"]["player_client"] = [client] + [
                c for c in _YT_CLIENTS if c != client
            ]
            opts["extract_flat"] = "in_playlist" if flat_playlist else False
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False, process=False)
                if info is None:
                    raise ExtractorError(f"No info for {url}")
                return MediaInfo.from_info(info)
            except (DownloadError, ExtractorError) as exc:
                log.warning("Metadata extraction failed with client=%s: %s", client, exc)
                last_exc = exc
        raise RuntimeError(f"All extractor clients failed for {url}: {last_exc}")

    return await loop.run_in_executor(_executor, _run)


# ---------------------------------------------------------------------------
# Format selector building with graceful fallback
# ---------------------------------------------------------------------------

def build_format_selector(
    download_type: str,
    quality: Optional[str],
    container: Optional[str],
    audio_format: Optional[str],
) -> list[str]:
    """Return an ordered list of format selectors to try in turn.

    The engine attempts each selector; the first that produces a successful
    download wins. This avoids the "Requested format not available" crash by
    always providing a progressive ``best`` fallback at the end of the list.
    """
    if download_type == "audio":
        primary = "bestaudio/best"
        return [primary, "best"]

    if download_type in {"thumbnail", "captions"}:
        return ["best"]  # selectors are mostly irrelevant here

    selectors: list[str] = []
    if quality and quality not in {"best", "bestvideo+bestaudio"}:
        # quality may be a height ("1080") or full selector
        if quality.isdigit():
            h = int(quality)
            ext_pref = f"[ext={container}]" if container and container != "mkv" else ""
            selectors.append(f"bestvideo[height<={h}]{ext_pref}+bestaudio/best[height<={h}]")
            selectors.append(f"bestvideo[height<={h}]+bestaudio/best[height<={h}]")
        else:
            selectors.append(quality)

    # Always include the safe defaults at the end.
    selectors.extend([
        "bestvideo+bestaudio/best",
        "best",
        "worst",  # ultimate fallback so something downloads instead of crashing
    ])
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for s in selectors:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


# ---------------------------------------------------------------------------
# Blocking download (run in an executor)
# ---------------------------------------------------------------------------

ProgressCb = Callable[[dict[str, Any]], None]


def _build_download_options(
    *,
    output_template: str,
    selector: str,
    download_type: str,
    container: Optional[str],
    audio_format: Optional[str],
    audio_quality: Optional[str],
    subtitle_langs: Optional[str],
    write_subs: bool,
    write_auto_subs: bool,
    embed_subs: bool,
    embed_thumb: bool,
    embed_meta: bool,
    embed_chapters: bool,
    playlist_start: Optional[int],
    playlist_end: Optional[int],
    progress_hook: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    opts = _base_options()
    opts.update({
        "outtmpl": output_template,
        "format": selector,
        "noprogress": False,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [progress_hook],
    })
    if download_type == "audio":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format or "mp3",
            "preferredquality": audio_quality or "0",
        }]
        if embed_thumb:
            opts["writethumbnail"] = True
            opts.setdefault("postprocessors", []).append({"key": "EmbedThumbnail"})
        if embed_meta:
            opts.setdefault("postprocessors", []).append({"key": "FFmpegMetadata"})
    else:
        if container and container != "mkv":
            opts["merge_output_format"] = container
        else:
            opts["merge_output_format"] = container or "mp4"
        pps: list[dict[str, Any]] = []
        if embed_thumb:
            opts["writethumbnail"] = True
            pps.append({"key": "EmbedThumbnail"})
        if embed_meta:
            pps.append({"key": "FFmpegMetadata"})
        if embed_chapters:
            pps.append({"key": "FFmpegMetadata", "add_chapters": True})
        if pps:
            opts["postprocessors"] = pps

    if write_subs or write_auto_subs or embed_subs:
        opts["writesubtitles"] = write_subs or embed_subs
        opts["writeautomaticsub"] = write_auto_subs
        opts["subtitleslangs"] = [s.strip() for s in (subtitle_langs or "en").split(",") if s.strip()]
        if embed_subs:
            opts.setdefault("postprocessors", []).append({"key": "FFmpegEmbedSubtitle"})

    if playlist_start:
        opts["playliststart"] = playlist_start
    if playlist_end:
        opts["playlistend"] = playlist_end
    return opts


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)


def submit_download(
    *,
    url: str,
    output_template: str,
    download_type: str,
    quality: Optional[str],
    container: Optional[str],
    audio_format: Optional[str],
    audio_quality: Optional[str],
    subtitle_langs: Optional[str],
    write_subs: bool,
    write_auto_subs: bool,
    embed_subs: bool,
    embed_thumb: bool,
    embed_meta: bool,
    embed_chapters: bool,
    playlist_start: Optional[int],
    playlist_end: Optional[int],
    progress_cb: ProgressCb,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Run a download synchronously. Intended to be called via ``run_in_executor``.

    Returns the final ``info_dict`` from yt-dlp once the download finishes.
    Raises the last extractor error if every selector fallback fails.
    """

    def hook(d: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise DownloadError("Cancelled")
        try:
            progress_cb(d)
        except Exception:  # pragma: no cover — never let UI errors kill the DL
            log.exception("Progress callback raised")

    selectors = build_format_selector(download_type, quality, container, audio_format)
    last_exc: Optional[Exception] = None
    for selector in selectors:
        opts = _build_download_options(
            output_template=output_template,
            selector=selector,
            download_type=download_type,
            container=container,
            audio_format=audio_format,
            audio_quality=audio_quality,
            subtitle_langs=subtitle_langs,
            write_subs=write_subs,
            write_auto_subs=write_auto_subs,
            embed_subs=embed_subs,
            embed_thumb=embed_thumb,
            embed_meta=embed_meta,
            embed_chapters=embed_chapters,
            playlist_start=playlist_start,
            playlist_end=playlist_end,
            progress_hook=hook,
        )
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if info is None:
                raise DownloadError("yt-dlp returned no info")
            return info
        except DownloadError as exc:
            msg = str(exc)
            if "Cancelled" in msg:
                raise
            log.warning("Selector %r failed: %s — trying next fallback", selector, exc)
            last_exc = exc
            continue
    raise RuntimeError(f"All format fallbacks exhausted: {last_exc}")


def get_executor() -> ThreadPoolExecutor:
    return _executor
