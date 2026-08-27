"""Persist the preemptive-sweep latch across runner restarts.

The HALT sentinel is a file, so it survives a process restart; the runner's
``_flatten_fired`` flag does not. Without a persisted latch, a restart with
flatten orders still working replays the whole sweep on the next tick: a fresh
cancel pass plus a new market order per position, which can flip the account
from long to net short. This module records the sweep's firing on disk, bound
to the halt *episode* that caused it.

The latch lives next to the per-broker HALT sentinel at
``<runtime_root>/live/<broker>/FLATTEN_FIRED`` and carries the episode identity
of the halt that was tripped when the sweep fired: the sentinel's ``tripped_at``
when it has one, otherwise the sentinel file's mtime. A later trip writes a new
sentinel (fresh ``tripped_at`` / fresh mtime), so a stale latch from a previous
episode never suppresses a new one; clearing HALT and re-tripping therefore
re-arms the sweep without anyone deleting the latch.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.live.halt import broker_halt_path, halt_path, read_halt
from src.live.paths import broker_dir

_LATCH_FILENAME = "FLATTEN_FIRED"


def latch_path(broker: str) -> Path:
    """Return the per-broker sweep latch path (not created here)."""
    return broker_dir(broker) / _LATCH_FILENAME


def _halt_episode(broker: str) -> str | None:
    """Return the identity of the currently tripped halt episode, if any.

    The per-broker sentinel wins when both exist, mirroring how a targeted
    halt is the one this broker's runner reacts to. ``tripped_at`` is the
    identity when the sentinel carries it; a hand-touched sentinel with no
    readable payload falls back to the file mtime, which still changes on
    every fresh ``touch``.
    """
    for path, payload in (
        (broker_halt_path(broker), read_halt(broker)),
        (halt_path(), read_halt()),
    ):
        if payload is None:
            continue
        tripped_at = payload.get("tripped_at")
        if tripped_at:
            return str(tripped_at)
        try:
            return f"mtime:{path.stat().st_mtime_ns}"
        except OSError:
            continue
    return None


def sweep_already_fired(broker: str) -> bool:
    """Return True when the sweep already fired for the current halt episode."""
    episode = _halt_episode(broker)
    if episode is None:
        return False
    try:
        record = json.loads(latch_path(broker).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(record, dict) and record.get("episode") == episode


def mark_sweep_fired(broker: str) -> None:
    """Record that the sweep fired for the current halt episode.

    Atomic write (same-directory temp file + ``os.replace``), same contract
    as the HALT sentinel. A no-op when no halt is tripped, since there is no
    episode to bind the record to.
    """
    episode = _halt_episode(broker)
    if episode is None:
        return
    record: dict[str, Any] = {
        "episode": episode,
        "fired_at": datetime.now(timezone.utc).isoformat(),
    }
    path = latch_path(broker)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".flatten-fired-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
