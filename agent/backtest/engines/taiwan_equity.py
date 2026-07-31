"""Taiwan (TWSE / TPEx) cash-equity backtest engine.

Market rules:
  - Trading unit: 1 board lot = 1,000 shares (整股). Odd-lot (零股) sessions are
    a separate venue and are not modelled; ``round_size`` floors to whole lots.
  - Price limit: ±10% from the previous close.
  - Day trading (當日沖銷) is permitted for eligible listed stocks, so unlike a
    T+1 delivery market there is no same-bar sell restriction by default.
  - Short selling (融券/借券) is available but restricted and carries separate
    fees; disabled by default, enable with ``allow_short=True``.

Cost stack (both figures are calibration knobs — brokers discount heavily and
the exchange revises rates):
  - 手續費 (brokerage): 0.1425% per side, × broker discount, with a per-order
    minimum (commonly NT$20)                    [tw_brokerage / tw_discount /
                                                 tw_min_commission]
  - 證交稅 (securities transaction tax): 0.3% on **sell only**. A pure
    day-trading strategy pays the halved 0.15% rate — set ``tw_tax=0.0015`` to
    model that. The engine does not auto-detect same-day round trips, because
    the commission hook has no entry-date context.        [tw_tax]
"""

from __future__ import annotations

import pandas as pd

from backtest.engines.base import BaseEngine
from backtest.engines.china_a import _blocked_by_limit


SHARES_PER_LOT = 1000

# Cost defaults
TW_BROKERAGE = 0.001425      # statutory maximum per side
TW_DISCOUNT = 0.6            # typical retail e-broker discount (6 折)
TW_MIN_COMMISSION = 20.0     # NT$ per order
TW_TAX = 0.003               # 證交稅, sell side only (當沖 = 0.0015)

PRICE_LIMIT = 0.10

# TWSE/TPEx 股票升降單位 (tick): (lower bound inclusive, tick). Every coarser
# tick is a whole multiple of every finer one, so a price on a coarse grid is
# already on the finer grids below it.
STOCK_TICK_TABLE: tuple[tuple[float, float], ...] = (
    (0.0, 0.01),
    (10.0, 0.05),
    (50.0, 0.10),
    (100.0, 0.50),
    (500.0, 1.00),
    (1000.0, 5.00),
)

# 受益憑證 (ETF/ETN) run a separate, finer two-step table. Taiwan ETF codes are
# the ones beginning ``00`` (0050, 006208, 00632R).
ETF_TICK_TABLE: tuple[tuple[float, float], ...] = (
    (0.0, 0.01),
    (50.0, 0.05),
)

# Half the finest tick, so comparisons tolerate binary float error without ever
# spanning a real price step.
_PRICE_EPS = 0.005


def _tick_table(symbol: str) -> tuple[tuple[float, float], ...]:
    """Return the tick table that applies to *symbol*."""
    code = symbol.split(".")[0] if "." in symbol else symbol
    return ETF_TICK_TABLE if code.startswith("00") else STOCK_TICK_TABLE


def twse_tick_size(symbol: str, price: float) -> float:
    """Return the tick unit applicable to *symbol* at *price*.

    Args:
        symbol: TWSE/TPEx symbol; the ``00`` prefix selects the ETF table.
        price: Price in NT$. Non-positive input returns the finest tick.

    Returns:
        The tick unit for the band containing *price*.
    """
    table = _tick_table(symbol)
    tick = table[0][1]
    for lower, unit in table:
        if price >= lower - _PRICE_EPS:
            tick = unit
        else:
            break
    return tick


def twse_round_down(symbol: str, price: float) -> float:
    """Truncate *price* down onto *symbol*'s tick grid."""
    tick = twse_tick_size(symbol, price)
    return round(int((price + _PRICE_EPS * tick) / tick) * tick, 4)


def twse_round_up(symbol: str, price: float) -> float:
    """Round *price* up onto *symbol*'s tick grid, never below one tick."""
    tick = twse_tick_size(symbol, price)
    steps = int((price - _PRICE_EPS * tick) / tick)
    if steps * tick < price - _PRICE_EPS * tick:
        steps += 1
    return round(max(steps, 1) * tick, 4)


class TaiwanEquityEngine(BaseEngine):
    """TWSE / TPEx cash-equity engine.

    Config keys (all optional; defaults shown in the module docstring):
      - allow_short: bool, default False
      - price_limit: fraction or None, default 0.10
      - slippage: default 0.001
      - lot_size: shares per lot, default 1000 (set 1 to model 零股)
      - tw_brokerage / tw_discount / tw_min_commission / tw_tax
    """

    def __init__(self, config: dict):
        config = {**config, "leverage": 1.0}   # cash equity: no leverage
        super().__init__(config)
        self.allow_short: bool = bool(config.get("allow_short", False))
        self.price_limit = config.get("price_limit", PRICE_LIMIT)
        self.slippage_rate: float = config.get("slippage", 0.001)
        self.lot_size: int = int(config.get("lot_size", SHARES_PER_LOT))
        self.tw_brokerage: float = config.get("tw_brokerage", TW_BROKERAGE)
        self.tw_discount: float = config.get("tw_discount", TW_DISCOUNT)
        self.tw_min_commission: float = config.get("tw_min_commission", TW_MIN_COMMISSION)
        self.tw_tax: float = config.get("tw_tax", TW_TAX)

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Taiwan cash-equity execution rules.

        Args:
            symbol: TWSE/TPEx symbol (e.g. ``2330.TW``).
            direction: 1 (buy), -1 (short), 0 (sell/close).
            bar: Current bar (needs ``close`` + ``pre_close``/``pct_chg`` for
                the price-limit check).

        Returns:
            True if the trade is allowed.
        """
        # 1. Short selling: 融券 is restricted; off unless explicitly enabled.
        if direction == -1 and not self.allow_short:
            return False

        # 2. No same-bar sell restriction: 當日沖銷 is permitted.

        # 3. Price limit ±10%, tested at execution time (see _blocked_by_limit):
        #    the band comes off a base price known before the order (pre_close
        #    or the prior close), compared against this bar's prospective fill.
        if not self.price_limit:
            return True
        pos = self.positions.get(symbol) if direction == 0 else None
        return not _blocked_by_limit(
            self,
            symbol,
            direction,
            bar,
            float(self.price_limit),
            position_direction=pos.direction if pos is not None else None,
        )

    def round_size(self, raw_size: float, price: float) -> float:
        """Floor to whole board lots (1,000 shares unless ``lot_size`` overrides)."""
        if self.lot_size <= 1:
            return float(max(int(raw_size), 0))
        lots = int(raw_size) // self.lot_size
        return float(max(lots * self.lot_size, 0))

    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        """Brokerage (both sides, with minimum) plus 證交稅 on the sell side.

        ``_direction`` is unused — the buy/sell asymmetry that matters here is
        carried by ``is_open``.
        """
        notional = size * price
        commission = max(
            notional * self.tw_brokerage * self.tw_discount,
            self.tw_min_commission,
        )
        if not is_open:
            commission += notional * self.tw_tax    # 證交稅: sell only
        return commission

    def limit_band(self, symbol: str, bar: pd.Series, limit: float):
        """The ±10% band, with both edges snapped inside the tick grid.

        TWSE publishes 漲停價 / 跌停價 on the grid: the raw band price is moved
        to the nearest tick *inside* the band, so the tradeable range never
        widens. Without this the band edges are prices the exchange cannot
        quote, and a fill can be judged against one.
        """
        band = super().limit_band(symbol, bar, limit)
        if band is None:
            return None
        lower, upper = band
        return twse_round_up(symbol, lower), twse_round_down(symbol, upper)

    def apply_slippage(self, price: float, direction: int) -> float:
        """Slippage against the order, then snapped onto the tick grid.

        Rounding goes away from the trader — a buy pays the next tick up, a
        sell receives the next tick down — so quantisation can never flatter
        the result. ``direction`` 0 is a close, which BaseEngine books with the
        opposite of the position direction before calling here, so every path
        arrives with a real side.
        """
        slipped = price * (1 + direction * self.slippage_rate)
        symbol = getattr(self, "_active_symbol", "") or ""
        if direction > 0:
            return twse_round_up(symbol, slipped)
        return twse_round_down(symbol, slipped)
