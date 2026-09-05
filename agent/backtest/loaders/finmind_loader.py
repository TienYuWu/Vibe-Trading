"""FinMind loader — Taiwan equities (TWSE / TPEx) and TAIFEX futures.

FinMind is the practical free source for Taiwan market data: `yfinance` carries
TWSE cash equities as ``NNNN.TW`` but has no TAIFEX futures or options at all.

Datasets used:
  - ``TaiwanStockPrice``   — cash equities, ``data_id`` = 股號 (e.g. ``2330``)
  - ``TaiwanFuturesDaily`` — futures, ``data_id`` = ``TX`` (大台) / ``MTX`` (小台)
  - ``TaiwanOptionDaily``  — options, ``data_id`` = ``TXO``

Auth: a free account token in ``FINMIND_TOKEN`` raises the hourly request cap.
Anonymous access works but is throttled hard.

Contract series collapsing: the futures/options daily tables carry every listed
contract (and both day and after-hours sessions) per date. This loader keeps, per
date, the single most-traded row — effectively the front-month / most-liquid
strike — so downstream engines see one continuous series. Trading a specific
delivery month or strike needs an explicit contract filter, which this loader
does not yet expose.
"""

from __future__ import annotations

import logging
import time

import pandas as pd
import requests

from backtest.loaders.base import validate_date_range, validate_ohlc
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
REQUEST_TIMEOUT = 30

# FinMind data_id aliases: our canonical product code -> FinMind's identifier.
_FUTURES_DATA_ID: dict[str, str] = {
    "TXF": "TX",    # 臺股期貨 (大台)
    "MXF": "MTX",   # 小型臺指期貨 (小台)
    "TMF": "TMF",   # 微型臺指期貨
    "TE": "TE",     # 電子期貨
    "TF": "TF",     # 金融期貨
}
_OPTION_DATA_ID: dict[str, str] = {"TXO": "TXO"}

# Taiwan indices. Keyed on the whole symbol rather than its letters because
# ``TAIEX`` would otherwise be read as a futures product code.
_INDEX_DATA_ID: dict[str, str] = {
    "TAIEX": "TAIEX",
    "^TWII": "TAIEX",     # the Yahoo spelling, accepted so a caller need not know ours
    "TAIEX.TW": "TAIEX",
}

_OHLCV = ["open", "high", "low", "close", "volume"]

# Optional columns, emitted only when the dataset carries them.
#   amount     — NT$ turnover (equities). Alphas that need adv/turnover skip the
#                symbol entirely without it.
#   settle     — daily settlement (futures). TAIFEX sets the ±10% band off the
#                PREVIOUS settlement, so ``pre_settle`` is what the engine's
#                price-limit check wants; without it the band silently falls
#                back to the previous close.
_EQUITY_EXTRA = ["amount"]
_FUTURES_EXTRA = ["settle", "pre_settle"]


@register
class DataLoader:
    """FinMind loader for Taiwan equities and TAIFEX derivatives."""

    name = "finmind"
    markets = {"tw_equity", "tw_futures"}
    requires_auth = False   # token optional (raises the rate cap), not required

    def __init__(self) -> None:
        from src.config.accessor import get_env_config

        self._token = get_env_config().data.finmind_token.strip()

    def _auth_headers(self) -> dict[str, str]:
        """Authorization header carrying the token, empty when anonymous.

        The token travels in a header rather than the query string on purpose.
        ``requests`` puts the full request URL into every ``HTTPError`` it
        raises, and those propagate into log lines and agent tool results — a
        token in the query string is a token in the logs.
        """
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def is_available(self) -> bool:
        """FinMind serves anonymous traffic, so availability is a reachability check."""
        try:
            resp = requests.get(
                FINMIND_URL,
                params={
                    "dataset": "TaiwanStockPrice",
                    "data_id": "2330",
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                },
                headers=self._auth_headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.debug("finmind unavailable: %s", exc)
            return False

    def fetch(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: list[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch daily OHLCV for Taiwan symbols.

        Args:
            codes: Symbols — ``2330`` / ``2330.TW`` for equities, ``TXF`` /
                ``MXF`` / ``TXO`` (optionally with a delivery suffix) for
                derivatives.
            start_date: ``YYYY-MM-DD``.
            end_date: ``YYYY-MM-DD``.
            interval: Only ``1D`` is supported; FinMind's free tier is daily.
            fields: Ignored — the OHLCV set is always returned.

        Returns:
            ``{symbol: DataFrame}`` indexed by ``trade_date`` with
            ``open/high/low/close/volume``. Symbols that return nothing are
            omitted rather than mapped to an empty frame.

        Raises:
            ValueError: Invalid date range or a non-daily interval.
        """
        validate_date_range(start_date, end_date)
        if interval not in ("1D", "1d", "D", "day", "daily"):
            raise ValueError(f"finmind supports daily bars only, got interval={interval!r}")

        out: dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                frame = self._fetch_one(code, start_date, end_date)
            except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the batch
                logger.warning("finmind fetch failed for %s: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                out[code] = frame
        return out

    # ── internals ──

    def _fetch_one(self, code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        dataset, data_id, is_derivative = _resolve_dataset(code)
        rows = self._request(dataset, data_id, start_date, end_date)
        if not rows:
            return None
        frame = (
            _normalize_derivative(rows) if is_derivative else _normalize_equity(rows)
        )
        if frame.empty:
            return None
        extra = _FUTURES_EXTRA if is_derivative else _EQUITY_EXTRA
        keep = _OHLCV + [c for c in extra if c in frame.columns]
        frame = frame[keep].dropna(subset=["open", "high", "low", "close"])
        return validate_ohlc(frame)

    def _request(self, dataset: str, data_id: str, start_date: str, end_date: str) -> list[dict]:
        params = {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        resp = requests.get(
            FINMIND_URL,
            params=params,
            headers=self._auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 200:
            msg = str(body.get("msg", body))
            # FinMind caps response size per request for the wide news/tick
            # datasets; the daily price tables are fine for multi-year ranges.
            raise RuntimeError(f"FinMind error for {dataset}/{data_id}: {msg[:160]}")
        time.sleep(0.2)   # be polite to a free endpoint
        return body.get("data", []) or []


def _resolve_dataset(code: str) -> tuple[str, str, bool]:
    """Map a symbol to ``(dataset, data_id, is_derivative)``."""
    base = code.split(".")[0].upper()
    product = "".join(ch for ch in base if ch.isalpha())

    if product in _OPTION_DATA_ID:
        return "TaiwanOptionDaily", _OPTION_DATA_ID[product], True
    if product in _FUTURES_DATA_ID:
        return "TaiwanFuturesDaily", _FUTURES_DATA_ID[product], True
    # TAIEX (發行量加權股價指數). FinMind serves it from the equity price table
    # under a non-numeric data_id, so it must be matched before the numeric
    # fallback below. Volume and turnover are market-wide totals, not a
    # tradeable quantity -- the index itself cannot be bought.
    if base in _INDEX_DATA_ID:
        return "TaiwanStockPrice", _INDEX_DATA_ID[base], False
    # Cash equity: FinMind wants the bare numeric code (2330.TW -> 2330).
    return "TaiwanStockPrice", code.split(".")[0], False


def _normalize_equity(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame = frame.rename(
        columns={
            "date": "trade_date",
            "max": "high",
            "min": "low",
            "Trading_Volume": "volume",
            # NT$ turnover, raw currency — no Tushare-style 千元 scaling, which
            # is why equity_tw shares the equity_us vwap treatment.
            "Trading_money": "amount",
        }
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    for col in _OHLCV + _EQUITY_EXTRA:
        if col not in frame.columns:
            frame[col] = pd.NA
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.set_index("trade_date").sort_index()


def _normalize_derivative(rows: list[dict]) -> pd.DataFrame:
    """Collapse multi-contract rows to the most-traded contract per date."""
    frame = pd.DataFrame(rows)
    frame = frame.rename(
        columns={
            "date": "trade_date",
            "max": "high",
            "min": "low",
            "settlement_price": "settle",
        }
    )
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    for col in _OHLCV + ["settle"]:
        if col not in frame.columns:
            frame[col] = pd.NA
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    # After-hours rows and settlement placeholders show up as zero/NaN prices.
    frame = frame[frame["close"].fillna(0) > 0]
    if frame.empty:
        return frame

    frame["volume"] = frame["volume"].fillna(0)
    # Highest volume wins per date; stable sort keeps the first on ties.
    frame = frame.sort_values(["trade_date", "volume"], ascending=[True, False])
    frame = frame.drop_duplicates(subset="trade_date", keep="first").set_index(
        "trade_date"
    ).sort_index()

    # TAIFEX sets the daily band off the PREVIOUS settlement, so the engine
    # needs it as its own column. Derived here rather than in the engine because
    # only the loader knows the contract's own calendar; a zero/blank settlement
    # (the exchange publishes those on thin days) must not become a base price.
    settle = frame["settle"].where(frame["settle"] > 0)
    frame["pre_settle"] = settle.shift(1)
    return frame


# ── Listing lookup (symbol search) ──
#
# Kept beside the loader because it hits the same host with the same optional
# token, but deliberately module-level rather than a DataLoader method: symbol
# search resolves an identity, it does not fetch bars, and importing the loader
# class for it would drag the whole fetch path into the tool.

# TWSE prints ordinary shares and ETFs; TPEx is the over-the-counter venue.
# Every other FinMind ``type`` (emerging board, warrants) is left unmapped so a
# candidate is skipped rather than emitted under a venue it does not trade on.
_LISTING_SUFFIX_BY_TYPE = {"twse": "TW", "tpex": "TWO"}

_listing_cache: dict[str, list[dict]] | None = None


def _load_listing_table() -> dict[str, list[dict]]:
    """Fetch and index FinMind's Taiwan listing table, once per process.

    Returns:
        ``stock_id`` -> list of listing rows. Empty when the fetch fails, which
        the caller reports as "no candidate" rather than an error.
    """
    global _listing_cache
    if _listing_cache is not None:
        return _listing_cache

    from src.config.accessor import get_env_config

    token = get_env_config().data.finmind_token.strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(
        FINMIND_URL,
        params={"dataset": "TaiwanStockInfo"},
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise RuntimeError(f"FinMind TaiwanStockInfo error: {str(body.get('msg', body))[:160]}")

    table: dict[str, list[dict]] = {}
    for row in body.get("data", []) or []:
        code = str(row.get("stock_id") or "").strip().upper()
        suffix = _LISTING_SUFFIX_BY_TYPE.get(str(row.get("type") or "").strip().lower())
        if not code or not suffix:
            continue
        table.setdefault(code, []).append({
            "code": code,
            "suffix": suffix,
            "name": str(row.get("stock_name") or "").strip(),
            "type": "etf" if code.startswith("00") else "equity",
        })
    _listing_cache = table
    return table


def fetch_listing(code: str) -> list[dict]:
    """Return listing rows for an exact Taiwan listing code.

    Args:
        code: Bare listing code, e.g. ``2330`` or ``00632R``.

    Returns:
        Rows with ``code``, ``suffix`` (``TW``/``TWO``), ``name`` and ``type``.
        Empty when the code is not listed in Taiwan.
    """
    return list(_load_listing_table().get((code or "").strip().upper(), []))


def reset_listing_cache() -> None:
    """Drop the cached listing table. For tests."""
    global _listing_cache
    _listing_cache = None
