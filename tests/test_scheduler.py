"""Tests for the opt-in in-process scheduler."""
from __future__ import annotations

from threading import Event, Thread

from tokensql.scheduler import OfficialPriceScheduler


def test_scheduler_is_disabled_by_default_and_runs_once():
    calls = []
    scheduler = OfficialPriceScheduler(lambda: calls.append("run") or 7)

    assert not scheduler.running
    assert calls == []
    result = scheduler.run_once()
    assert result.succeeded
    assert result.value == 7
    assert calls == ["run"]
    assert not scheduler.running


def test_job_failure_is_returned_not_raised():
    def fail():
        raise RuntimeError("boom")

    result = OfficialPriceScheduler(fail).run_once()
    assert result.status == "failed"
    assert result.error == "RuntimeError: boom"


def test_run_once_is_non_reentrant():
    entered = Event()
    release = Event()

    def blocking_job():
        entered.set()
        assert release.wait(2)
        return "done"

    scheduler = OfficialPriceScheduler(blocking_job)
    results = []
    worker = Thread(target=lambda: results.append(scheduler.run_once()))
    worker.start()
    assert entered.wait(1)

    overlapping = scheduler.run_once()
    assert overlapping.skipped
    assert "already running" in overlapping.error

    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert results[0].succeeded


def test_interval_continues_after_error_and_can_be_stopped():
    reached_second_call = Event()
    calls = []

    def flaky_job():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise ValueError("first run fails")
        reached_second_call.set()
        return len(calls)

    scheduler = OfficialPriceScheduler(flaky_job)
    assert scheduler.start(0.01, run_immediately=True)
    assert not scheduler.start(0.01)
    assert reached_second_call.wait(1)
    assert scheduler.stop(timeout=1)
    assert not scheduler.running
    assert len(calls) >= 2
