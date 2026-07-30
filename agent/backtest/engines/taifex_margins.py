"""TAIFEX margin table, refreshed from the exchange's official OpenAPI.

TAIFEX rescales margins as the index moves, so hard-coded amounts rot silently:
the 臺股期貨 figure that was right near a 15,000 index implies a ~2% margin at
45,000, which no exchange would set. This module reads the authoritative table
instead and keeps the constants only as an offline fallback.

Source: https://openapi.taifex.com.tw/v1/IndexFuturesAndOptionsMargining
        ("保證金一覽表-股價指數類"), fields Contract / ClearingMargin /
        MaintenanceMargin / InitialMargin / Date.

Behaviour:
  - First lookup fetches once and caches to
    ``~/.vibe-trading/cache/taifex/index_margins.json`` for ``CACHE_TTL_S``.
  - Any failure (offline, timeout, schema change) falls back to
    ``FALLBACK_INITIAL_MARGIN`` — a backtest must never die because the
    exchange website is down.
  - Set ``VIBE_TRADING_TAIFEX_MARGIN_AUTOUPDATE=0`` to pin the fallback table
    (used by tests, and by anyone who wants byte-reproducible runs).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MARGIN_API_URL = "https://openapi.taifex.com.tw/v1/IndexFuturesAndOptionsMargining"
REQUEST_TIMEOUT_S = 8
CACHE_TTL_S = 24 * 3600
AUTOUPDATE_ENV = "VIBE_TRADING_TAIFEX_MARGIN_AUTOUPDATE"

# TAIFEX contract names -> our product codes. Names come from the API's
# ``Contract`` field verbatim.
#
# Deliberately limited to products whose contract multiplier is verified in
# ``taiwan_futures._MULTIPLIER``. The API also lists 小型電子/小型金融/櫃買/
# 中型100/半導體30 and the TXO risk-margin A/B/C values; pulling their margins in
# without a matching multiplier would silently price PnL off the wrong
# points-to-NT$ factor, which is worse than not supporting the product.
_CONTRACT_TO_PRODUCT: dict[str, str] = {
    "臺股期貨": "TXF",
    "小型臺指": "MXF",
    "微型臺指期貨": "TMF",
    "電子期貨": "TE",
    "金融期貨": "TF",
}

# Offline fallback: initial margin (原始保證金) in NT$ per contract.
# Snapshot of the API table dated 2026-07-29. Only used when the fetch fails.
FALLBACK_INITIAL_MARGIN: dict[str, float] = {
    "TXF": 636000.0,
    "MXF": 159000.0,
    "TMF": 31800.0,
    "TE": 889000.0,
    "TF": 144000.0,
}

_cache: dict[str, float] | None = None


def autoupdate_enabled() -> bool:
    """Whether the live table may be fetched (default yes)."""
    return os.getenv(AUTOUPDATE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def cache_path() -> Path:
    return Path.home() / ".vibe-trading" / "cache" / "taifex" / "index_margins.json"


def parse_margin_rows(rows: list) -> dict[str, float]:
    """Map API rows to ``{product_code: initial_margin}``.

    Unknown contract names and unparseable amounts are skipped rather than
    failing the whole table — TAIFEX adds products over time.
    """
    out: dict[str, float] = {}
    for row in rows:
        product = _CONTRACT_TO_PRODUCT.get(str(row.get("Contract", "")).strip())
        if not product:
            continue
        try:
            margin = float(str(row.get("InitialMargin", "")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if margin > 0:
            out[product] = margin
    return out


def _read_cache() -> dict[str, float] | None:
    path = cache_path()
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > CACHE_TTL_S:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        margins = payload.get("margins") or {}
        return {k: float(v) for k, v in margins.items()} or None
    except Exception as exc:  # noqa: BLE001 - a bad cache just means refetch
        logger.debug("taifex margin cache unreadable: %s", exc)
        return None


def _write_cache(margins: dict[str, float], as_of: str) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"as_of": as_of, "margins": margins}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("taifex margin cache write failed: %s", exc)


def fetch_initial_margins() -> tuple[dict[str, float], str]:
    """Fetch and parse the live table. Returns ``(margins, as_of_date)``.

    Raises:
        requests.RequestException / ValueError: propagated to the caller, which
            decides whether to fall back.
    """
    resp = requests.get(MARGIN_API_URL, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    rows = resp.json()
    margins = parse_margin_rows(rows)
    if not margins:
        raise ValueError("TAIFEX margin table parsed to zero known products")
    as_of = str(rows[0].get("Date", "")) if rows else ""
    return margins, as_of


def get_initial_margins() -> dict[str, float]:
    """Initial margin per product, live table preferred, fallback on any failure.

    Cached in-process after the first call, so a backtest does one fetch at most.
    """
    global _cache
    if _cache is not None:
        return _cache

    if not autoupdate_enabled():
        _cache = dict(FALLBACK_INITIAL_MARGIN)
        return _cache

    cached = _read_cache()
    if cached:
        _cache = {**FALLBACK_INITIAL_MARGIN, **cached}
        return _cache

    try:
        margins, as_of = fetch_initial_margins()
        _write_cache(margins, as_of)
        logger.info("TAIFEX margins refreshed (as of %s): %d products", as_of, len(margins))
        _cache = {**FALLBACK_INITIAL_MARGIN, **margins}
    except Exception as exc:  # noqa: BLE001 - never fail a backtest over this
        logger.warning(
            "TAIFEX margin fetch failed (%s); using the bundled 2026-07-29 snapshot", exc
        )
        _cache = dict(FALLBACK_INITIAL_MARGIN)
    return _cache


def reset_cache() -> None:
    """Drop the in-process cache (tests, or after changing the env gate)."""
    global _cache
    _cache = None
