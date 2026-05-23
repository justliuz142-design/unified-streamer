"""Background cleanup scheduler.

Removes finished downloads older than ``CLEANUP_AFTER_SECONDS`` (default
3 hours). Never touches files that belong to a job still in ``downloading``
or ``pending`` state, eliminating the classic race where the scheduler
deletes the file out from under the writer.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from config import settings
from queue_manager import DownloadQueue

log = logging.getLogger("cleanup")


class CleanupScheduler:
    def __init__(self, queue: DownloadQueue) -> None:
        self._queue = queue
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            log.info(
                "Cleanup scheduler started — files older than %ds will be removed",
                settings.cleanup_seconds,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:
                log.exception("Cleanup sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.cleanup_interval)
            except asyncio.TimeoutError:
                pass

    async def sweep(self) -> int:
        """Delete download directories whose jobs finished long enough ago."""
        snapshot = await self._queue.snapshot()
        jobs_by_id = {j["id"]: j for j in snapshot}
        cutoff = time.time() - settings.cleanup_seconds
        removed = 0
        root: Path = settings.download_dir
        if not root.exists():
            return 0
        for child in root.iterdir():
            if not child.is_dir():
                continue
            job = jobs_by_id.get(child.name)
            # Always skip active jobs (no race with the writer).
            if job and job["status"] in {"pending", "downloading"}:
                continue
            # Use the job's finished_at when available, else file mtime.
            ts = (job or {}).get("finished_at") or child.stat().st_mtime
            if ts and ts < cutoff:
                try:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                    log.info("Cleanup removed %s", child)
                except OSError as exc:
                    log.warning("Cannot remove %s: %s", child, exc)
        return removed
