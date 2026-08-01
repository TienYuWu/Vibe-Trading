"""Protective stops evaluated inside the bar.

A signal engine emits target weights computed from completed bars, so a stop
written there can only ever react to a close: "closed below -5%, therefore flat
from the next open". A real protective order fills when price *touches* it,
which is a within-bar event the target-weight path cannot express.

These rules run in the engine instead, against the bar's own high/low, from a
level that was fixed before the bar opened. The level never uses the bar it is
tested against — see :meth:`ChandelierStop.update`, which is called only after
the trigger check for that bar has already run.

Default rule is the Chandelier Exit (Chuck LeBeau): for a long, the highest
high reached since entry minus ``multiplier`` ATRs. It ratchets — the level
only ever moves in the trade's favour, because a stop that can retreat from
price is not protection.

Stops are opt-in. ``stop_rule`` defaults to ``"none"`` so an existing backtest
re-run produces identical numbers; set ``stop_rule="chandelier"`` in
``config.json`` to enable them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


VALID_STOP_RULES = ("none", "chandelier")

DEFAULT_ATR_PERIOD = 22
DEFAULT_MULTIPLIER = 3.0


@dataclass(frozen=True)
class StopConfig:
    """Protective-stop settings read from ``config.json``.

    Attributes:
        rule: ``"none"`` (default) or ``"chandelier"``.
        atr_period: Lookback for Wilder's ATR and the entry-anchored extreme.
        multiplier: How many ATRs the stop trails behind the extreme.
        take_profit_pct: Optional fixed target as a fraction of the entry price
            (``0.15`` = +15% for a long). ``None`` disables it.
    """

    rule: str = "none"
    atr_period: int = DEFAULT_ATR_PERIOD
    multiplier: float = DEFAULT_MULTIPLIER
    take_profit_pct: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self.rule != "none" or self.take_profit_pct is not None

    @classmethod
    def from_config(cls, config: dict) -> "StopConfig":
        """Build from a backtest config dict, validating the rule name.

        Raises:
            ValueError: On an unknown ``stop_rule``, a non-positive period or
                multiplier, or a non-positive ``take_profit_pct``. A silently
                ignored stop setting is worse than a refused run: the user
                would read the result as risk-managed when it is not.
        """
        rule = str(config.get("stop_rule", "none")).lower()
        if rule not in VALID_STOP_RULES:
            raise ValueError(
                f"stop_rule must be one of {VALID_STOP_RULES}, got {rule!r}"
            )

        period = int(config.get("stop_atr_period", DEFAULT_ATR_PERIOD))
        multiplier = float(config.get("stop_atr_multiplier", DEFAULT_MULTIPLIER))
        if period < 1:
            raise ValueError(f"stop_atr_period must be >= 1, got {period}")
        if multiplier <= 0:
            raise ValueError(f"stop_atr_multiplier must be > 0, got {multiplier}")

        raw_tp = config.get("take_profit_pct")
        take_profit = None if raw_tp is None else float(raw_tp)
        if take_profit is not None and take_profit <= 0:
            raise ValueError(f"take_profit_pct must be > 0, got {take_profit}")

        return cls(
            rule=rule,
            atr_period=period,
            multiplier=multiplier,
            take_profit_pct=take_profit,
        )


def wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range.

    True range is the largest of the bar's own span and the two gaps against
    the previous close, so it counts overnight moves the high-low span misses.
    Wilder's smoothing is an EMA with ``alpha = 1/period``, which is what the
    Chandelier Exit was defined against.

    Args:
        df: Frame carrying ``high``, ``low`` and ``close``.
        period: Smoothing length in bars.

    Returns:
        ATR indexed like *df*. Leading bars are NaN until one full period of
        true ranges exists, so a stop cannot be armed on a half-formed average.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


@dataclass
class ChandelierStop:
    """Ratcheting stop level for one open position.

    The level is only ever tightened (raised for a long, lowered for a short).
    ``level`` is ``None`` until the first :meth:`update` lands a usable ATR,
    which keeps the position unprotected rather than stopped at a fabricated
    price while history is still short.
    """

    direction: int
    multiplier: float
    extreme: Optional[float] = None
    level: Optional[float] = None

    def update(self, high: float, low: float, atr: float) -> None:
        """Fold one completed bar into the level for the NEXT bar.

        Args:
            high: The bar's high.
            low: The bar's low.
            atr: ATR at this bar; NaN before the period fills.
        """
        if self.direction == 1:
            self.extreme = high if self.extreme is None else max(self.extreme, high)
        else:
            self.extreme = low if self.extreme is None else min(self.extreme, low)

        if atr != atr or atr <= 0:  # NaN or non-positive: nothing to arm with
            return

        offset = self.multiplier * atr
        candidate = (
            self.extreme - offset if self.direction == 1 else self.extreme + offset
        )
        if self.level is None:
            self.level = candidate
        elif self.direction == 1:
            self.level = max(self.level, candidate)
        else:
            self.level = min(self.level, candidate)


def trigger_price(
    direction: int,
    level: float,
    bar_open: float,
    bar_high: float,
    bar_low: float,
    *,
    is_target: bool = False,
) -> Optional[float]:
    """Price a resting order at *level* would fill at during this bar.

    Args:
        direction: 1 for a long position, -1 for a short.
        level: The resting order's trigger price.
        bar_open: The bar's open.
        bar_high: The bar's high.
        bar_low: The bar's low.
        is_target: True for a take-profit (favourable side), False for a stop
            (adverse side). This flips which side of the level counts.

    Returns:
        The fill price, or None when the bar never reached the level. A bar
        that OPENS through the level fills at the open, not the level — the
        gap is the price the market actually offered. A bar that opens inside
        and touches later fills at the level.
    """
    # A long's stop sits below and its target above; a short's is the mirror.
    below = (direction == 1) != is_target
    if below:
        if bar_open <= level:
            return bar_open
        return level if bar_low <= level else None
    if bar_open >= level:
        return bar_open
    return level if bar_high >= level else None
