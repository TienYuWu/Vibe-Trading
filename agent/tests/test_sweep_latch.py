"""Tests for the restart-persistent preemptive-sweep latch.

The HALT sentinel is a file and survives restarts; the runner's in-memory
``_flatten_fired`` does not. The latch (src/live/runtime/sweep_latch.py) binds
the sweep's firing to the halt episode that caused it, so a restarted runner
with flatten orders still working does not replay the sweep, while a fresh
halt episode re-arms it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from src.live import paths
from src.live.halt import broker_halt_path, clear_halt, halt_path, trip_halt
from src.live.runtime import sweep_latch
from src.live.runtime.runner import LiveRunner

BROKER = "robinhood"


@pytest.fixture
def live_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the live runtime root at an isolated tmp dir."""
    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    return tmp_path


def _trip_with_timestamp(broker: str | None, tripped_at: str) -> None:
    path = broker_halt_path(broker) if broker else halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tripped_at": tripped_at, "by": "cli", "reason": "test"}),
        encoding="utf-8",
    )


def test_mark_then_fired_for_same_episode(live_root: Path) -> None:
    _trip_with_timestamp(BROKER, "2026-08-27T01:00:00+00:00")
    assert sweep_latch.sweep_already_fired(BROKER) is False
    sweep_latch.mark_sweep_fired(BROKER)
    assert sweep_latch.sweep_already_fired(BROKER) is True


def test_fresh_halt_episode_rearms(live_root: Path) -> None:
    _trip_with_timestamp(BROKER, "2026-08-27T01:00:00+00:00")
    sweep_latch.mark_sweep_fired(BROKER)
    assert sweep_latch.sweep_already_fired(BROKER) is True
    # The operator clears the halt and a later incident trips it again: the
    # latch from the first episode must not suppress the second sweep.
    clear_halt(BROKER)
    _trip_with_timestamp(BROKER, "2026-08-27T09:30:00+00:00")
    assert sweep_latch.sweep_already_fired(BROKER) is False


def test_mark_without_halt_is_noop(live_root: Path) -> None:
    sweep_latch.mark_sweep_fired(BROKER)
    assert not sweep_latch.latch_path(BROKER).exists()


def test_hand_touched_halt_binds_via_mtime(live_root: Path) -> None:
    # A bare `touch` produces a sentinel with no JSON payload; the latch must
    # still bind to the episode (via the file mtime) rather than never firing.
    path = broker_halt_path(BROKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    assert sweep_latch.sweep_already_fired(BROKER) is False
    sweep_latch.mark_sweep_fired(BROKER)
    assert sweep_latch.sweep_already_fired(BROKER) is True


def test_global_halt_episode_visible_to_broker(live_root: Path) -> None:
    _trip_with_timestamp(None, "2026-08-27T02:00:00+00:00")
    sweep_latch.mark_sweep_fired(BROKER)
    assert sweep_latch.sweep_already_fired(BROKER) is True


def test_corrupt_latch_falls_back_to_not_fired(live_root: Path) -> None:
    _trip_with_timestamp(BROKER, "2026-08-27T01:00:00+00:00")
    latch = sweep_latch.latch_path(BROKER)
    latch.parent.mkdir(parents=True, exist_ok=True)
    latch.write_text("{not json", encoding="utf-8")
    assert sweep_latch.sweep_already_fired(BROKER) is False


def _build_runner(live_root: Path, fired: list[str]) -> LiveRunner:
    """A runner whose only observable behavior is recording sweep invocations."""

    async def _agent_caller(session_id: str, prompt: str) -> Mapping[str, Any]:
        return {"status": "success"}

    def _submit(request: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    def _flatten(broker, submit, read_positions, read_open_orders):
        fired.append(broker)

    def _audit(event) -> Mapping[str, Any]:
        return {"audit_id": "a1"}

    return LiveRunner(
        BROKER,
        agent_caller=_agent_caller,
        reconcile_fn=lambda *a, **k: None,
        read_positions=list,
        read_balance=list,
        read_open_orders=list,
        write_audit_fn=_audit,
        halt_flag_fn=lambda broker: True,
        submit_fn=_submit,
        flatten_fn=_flatten,
        session_id="latch-test",
    )


def test_restart_does_not_replay_the_sweep(live_root: Path) -> None:
    _trip_with_timestamp(BROKER, "2026-08-27T01:00:00+00:00")
    fired: list[str] = []
    asyncio.run(_build_runner(live_root, fired).run_once())
    assert fired == [BROKER]
    # A new runner instance over the same runtime root (the restart): the
    # in-memory latch is gone, but the on-disk one suppresses the replay.
    asyncio.run(_build_runner(live_root, fired).run_once())
    assert fired == [BROKER]


def test_new_episode_after_restart_fires_again(live_root: Path) -> None:
    _trip_with_timestamp(BROKER, "2026-08-27T01:00:00+00:00")
    fired: list[str] = []
    asyncio.run(_build_runner(live_root, fired).run_once())
    clear_halt(BROKER)
    _trip_with_timestamp(BROKER, "2026-08-27T09:30:00+00:00")
    asyncio.run(_build_runner(live_root, fired).run_once())
    assert fired == [BROKER, BROKER]
