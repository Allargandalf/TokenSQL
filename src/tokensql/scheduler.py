"""Explicit, in-process scheduling for official price refreshes.

Importing or constructing this module never starts a thread.  It intentionally
has no system-cron or service-manager integration.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread, current_thread
from typing import Any


@dataclass(frozen=True)
class SchedulerRun:
    status: str
    started_at: datetime
    finished_at: datetime
    value: Any = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


class OfficialPriceScheduler:
    """Run a callable once or at an explicitly requested interval."""

    def __init__(
        self,
        job: Callable[[], Any],
        *,
        clock: Callable[[], datetime] | None = None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        if not callable(job):
            raise TypeError("job must be callable")
        self._job = job
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_factory = thread_factory
        self._run_lock = Lock()
        self._state_lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._interval_seconds: float | None = None
        self._last_run: SchedulerRun | None = None

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_run(self) -> SchedulerRun | None:
        with self._state_lock:
            return self._last_run

    def run_once(self) -> SchedulerRun:
        """Run without re-entry; job exceptions become an isolated result."""
        started = self._clock()
        if not self._run_lock.acquire(blocking=False):
            result = SchedulerRun(
                status="skipped",
                started_at=started,
                finished_at=self._clock(),
                error="job is already running",
            )
            self._remember(result)
            return result
        try:
            try:
                value = self._job()
            except Exception as exc:
                result = SchedulerRun(
                    status="failed",
                    started_at=started,
                    finished_at=self._clock(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                result = SchedulerRun(
                    status="completed",
                    started_at=started,
                    finished_at=self._clock(),
                    value=value,
                )
            self._remember(result)
            return result
        finally:
            self._run_lock.release()

    run_now = run_once

    def start(
        self,
        interval_seconds: float,
        *,
        run_immediately: bool = False,
    ) -> bool:
        """Start a daemon interval loop; return False when already running."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._interval_seconds = float(interval_seconds)
            self._stop_event.clear()
            thread = self._thread_factory(
                target=self._interval_loop,
                args=(bool(run_immediately),),
                name="tokensql-official-price-scheduler",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True

    start_interval = start

    def _interval_loop(self, run_immediately: bool) -> None:
        if run_immediately and not self._stop_event.is_set():
            self.run_once()
        while True:
            interval = self._interval_seconds
            if interval is None or self._stop_event.wait(interval):
                return
            # run_once catches job exceptions, so one failure cannot kill the loop.
            self.run_once()

    def stop(self, *, timeout: float | None = None) -> bool:
        """Stop only this in-process loop; no external scheduler is touched."""
        with self._state_lock:
            thread = self._thread
            if thread is None:
                return False
            self._stop_event.set()
        if thread is not current_thread():
            thread.join(timeout)
        with self._state_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._interval_seconds = None
        return not thread.is_alive()

    def close(self) -> None:
        self.stop()

    def _remember(self, result: SchedulerRun) -> None:
        with self._state_lock:
            self._last_run = result

    def __enter__(self) -> OfficialPriceScheduler:
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()


Scheduler = OfficialPriceScheduler
