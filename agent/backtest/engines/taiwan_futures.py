"""Taiwan (TAIFEX) index-futures backtest engine.

Market rules (TAIFEX):
  - T+0: open and close in the same session (day trading allowed)
  - Both directions: long and short both permitted
  - Price limit: ±10% from the previous settlement (stock-index products)
  - Contract multiplier: TXF=200, MXF=50, TMF=10 (NT$ per index point)
  - Margin: TAIFEX publishes an **absolute NT$ amount per contract**, not a
    percentage, and rescales it as the index moves — see ``_INITIAL_MARGIN``
    and the ``_calc_margin`` override
  - Minimum trading unit: 1 contract
  - Cost stack (both sides): 期交稅 (futures transaction tax) 0.002% of notional
    + broker commission per lot

NOTE: TAIFEX revises margins and the exchange revises tax rates periodically.
Every number below is a calibration knob — verify against the current TAIFEX
margin table and your broker's schedule before trusting absolute cost figures.
Margin figures here were read from the TAIFEX table on 2026-08-11; the tax rate
and per-lot commission default have NOT been re-verified against a current
schedule.

TXO (臺指選擇權) is deliberately **not** handled here: options need premium-based
margin and an option pricing/quote path, which lives in
``backtest.engines.options_portfolio``. Routing TXO through a futures engine
would silently mis-model it.
"""

from __future__ import annotations

import re

import pandas as pd

from backtest.engines.futures_base import FuturesBaseEngine
from backtest.engines.taifex_margins import get_initial_margins


# ── Contract multiplier (NT$ per index point) ──

_MULTIPLIER: dict[str, float] = {
    "TXF": 200.0,   # 臺股期貨 (大台)
    "MXF": 50.0,    # 小型臺指期貨 (小台)
    "TMF": 10.0,    # 微型臺指期貨 (微台)
    "TE": 4000.0,   # 電子期貨
    "TF": 1000.0,   # 金融期貨
}

# ── Initial margin, absolute NT$ per contract ──
#
# Read from TAIFEX's official OpenAPI at runtime and cached for a day — see
# ``taifex_margins``. Hard-coding is not viable: the exchange rescales margins
# as the index moves, so a constant that was right near a 15,000 index implies
# an impossible ~2% margin at 45,000. The bundled snapshot in that module is
# only the offline fallback.
#
# Maintenance margin (維持保證金) is fetched but not modelled: BaseEngine has no
# margin-call / forced-liquidation hook for futures, so acting on it would need
# an engine-level change first.

# ── Cost stack ──

# 期交稅: 股價類期貨契約 十萬分之二 per side, on notional.
FUTURES_TAX_RATE = 0.00002
# Broker commission per lot per side (NT$). Highly broker-dependent.
DEFAULT_COMMISSION_PER_LOT = 20.0

# Stock-index futures: ±10% of the previous settlement price.
PRICE_LIMIT = 0.10

# TAIFEX index futures are quoted in whole points, so rate-based slippage only
# approximates a 1-tick fill. ~1 point at a 45,000 index level.
DEFAULT_SLIPPAGE = 0.00002

# TAIEX level used only to turn an absolute NT$ margin into a plausible leverage
# for position sizing. Known products post their exchange margin directly (see
# ``_calc_margin``), so this matters only for unlisted contracts. It assumes a
# TAIEX-tracking product — TE (電子) and TF (金融) track different indices.
# Override via ``reference_index_level`` when the index moves materially.
REFERENCE_INDEX_LEVEL = 45000.0


def _extract_product(symbol: str) -> str:
    """Extract the TAIFEX product code from a futures symbol.

    Examples:
        'TXF202608.TAIFEX' -> 'TXF'
        'MXF2608'          -> 'MXF'
        'TXF'              -> 'TXF'

    Args:
        symbol: Futures symbol string.

    Returns:
        Upper-cased product code.
    """
    code = symbol.split(".")[0]
    m = re.match(r"([A-Za-z]+)", code)
    return (m.group(1) if m else code).upper()


class TaiwanFuturesEngine(FuturesBaseEngine):
    """TAIFEX index-futures engine (TXF / MXF / TMF / TE / TF).

    Config keys (all optional):
      - slippage: default 0.00005 (~1 index point at 20000)
      - commission_per_lot: NT$ per contract per side, default 20.0
      - futures_tax_rate: default 0.00002 (十萬分之二)
      - margin_override: absolute NT$ per contract, applied to every product
      - price_limit: fraction, default 0.10; falsy disables the check
    """

    def __init__(self, config: dict):
        # Leverage is only the fallback path: _calc_margin below uses TAIFEX's
        # absolute per-contract margin whenever the product is known. Derive a
        # plausible leverage from the first code so sizing stays sane for
        # unknown products.
        codes = config.get("codes", [])
        product = _extract_product(codes[0]) if codes else ""
        margin_abs = config.get("margin_override") or get_initial_margins().get(product)
        cm = _MULTIPLIER.get(product)
        reference = config.get("reference_index_level", REFERENCE_INDEX_LEVEL)
        if margin_abs and cm:
            # Implied leverage at a reference index level. _calc_margin below
            # uses the absolute NT$ margin for known products, so this only
            # affects sizing for unlisted ones.
            leverage = (float(reference) * cm) / float(margin_abs)
        else:
            leverage = 14.0   # ~TXF at 636k margin, 45k index
        config = {**config, "leverage": leverage}
        super().__init__(config)

        self.slippage_rate: float = config.get("slippage", DEFAULT_SLIPPAGE)
        self.commission_per_lot: float = config.get(
            "commission_per_lot", DEFAULT_COMMISSION_PER_LOT
        )
        self.futures_tax_rate: float = config.get("futures_tax_rate", FUTURES_TAX_RATE)
        self.price_limit = config.get("price_limit", PRICE_LIMIT)
        self._margin_override = config.get("margin_override")

    # ── Market rules ──

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """TAIFEX: T+0, long and short both allowed, ±10% limit enforced.

        Args:
            symbol: Futures code (e.g. ``TXF202608``).
            direction: 1 (long), -1 (short), 0 (close).
            bar: Current bar.

        Returns:
            True if the trade is allowed.
        """
        if not self.price_limit:
            return True
        pct_chg = _calc_pct_change(bar)
        if pct_chg is None:
            return True

        limit = float(self.price_limit)
        if direction == 1 and pct_chg >= limit - 0.001:
            return False                      # limit-up: cannot open long
        if direction == -1 and pct_chg <= -limit + 0.001:
            return False                      # limit-down: cannot open short
        if direction == 0:
            pos = self.positions.get(symbol)
            if pos is not None:
                if pos.direction == 1 and pct_chg <= -limit + 0.001:
                    return False              # cannot close long at limit-down
                if pos.direction == -1 and pct_chg >= limit - 0.001:
                    return False              # cannot close short at limit-up
        return True

    def round_size(self, raw_size: float, price: float) -> float:
        """Whole contracts only."""
        return float(max(int(raw_size), 0))

    # ── Costs ──

    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        """Broker commission per lot plus 期交稅, charged on both sides.

        ``_direction`` is unused: TAIFEX charges the same tax and fee opening
        and closing. ``is_open`` is likewise symmetric, kept for the base-class
        signature.
        """
        return self.calc_commission_for_symbol(self._active_symbol, size, price, is_open)

    def calc_commission_for_symbol(
        self, symbol: str, size: float, price: float, is_open: bool,
    ) -> float:
        """Symbol-aware cost: per-lot commission + notional-based tax.

        Args:
            symbol: Futures code.
            size: Number of contracts.
            price: Execution price in index points.
            is_open: True for an opening trade (unused; costs are symmetric).

        Returns:
            Cost in NT$.
        """
        cm = self.get_contract_multiplier(symbol)
        notional = size * price * cm
        return size * self.commission_per_lot + notional * self.futures_tax_rate

    def apply_slippage(self, price: float, direction: int) -> float:
        """Slippage always works against the order."""
        return price * (1 + direction * self.slippage_rate)

    # ── Contract mechanics ──

    def get_contract_multiplier(self, symbol: str) -> float:
        """NT$ per index point for this product (unknown -> 小台 as the safer default)."""
        return float(_MULTIPLIER.get(_extract_product(symbol), 50.0))

    def get_initial_margin(self, symbol: str) -> float | None:
        """Absolute NT$ initial margin per contract, or None if unknown.

        Reads the live TAIFEX table (cached for a day, falling back to the
        bundled snapshot offline).
        """
        if self._margin_override:
            return float(self._margin_override)
        margin = get_initial_margins().get(_extract_product(symbol))
        return float(margin) if margin is not None else None

    def _calc_margin(
        self, symbol: str, size: float, price: float, leverage: float,
    ) -> float:
        """TAIFEX charges a fixed NT$ margin per contract, not a percentage.

        Falls back to the multiplier/leverage formula for products missing from
        ``_INITIAL_MARGIN`` so an unlisted contract still sizes sensibly.
        """
        margin = self.get_initial_margin(symbol)
        if margin is not None:
            return size * margin
        return super()._calc_margin(symbol, size, price, leverage)


# ── Helpers ──


# TAIFEX reports a daily settlement price, so settle/pre_settle is the
# authoritative pair for the ±10% band (mirrors china_futures); close/pre_close
# is the fallback when a loader only carries OHLC.
def _calc_pct_change(bar: pd.Series):
    """Bar change fraction. Priority: settle/pre_settle > close/pre_close > pct_chg."""
    settle = bar.get("settle")
    pre_settle = bar.get("pre_settle")
    if settle is not None and pre_settle is not None and float(pre_settle) > 0:
        return (float(settle) - float(pre_settle)) / float(pre_settle)

    close = bar.get("close")
    pre_close = bar.get("pre_close")
    if close is not None and pre_close is not None and float(pre_close) > 0:
        return (float(close) - float(pre_close)) / float(pre_close)

    if "pct_chg" in bar.index:
        val = bar["pct_chg"]
        if pd.notna(val):
            return float(val) / 100.0
    return None
