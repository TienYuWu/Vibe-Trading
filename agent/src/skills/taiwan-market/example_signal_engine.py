"""Taiwan trend-following demo: Donchian breakout entry, engine-side stop exit.

Theme: buy strength. A close above the highest close of the prior ``entry_window``
bars is the entry; the position is then handed to the engine's Chandelier stop,
which trails it out. There is deliberately no exit rule here — that is the point
of the example.

Why the exit lives in config.json, not in this file:

    A signal engine only emits target weights per bar, so any exit written here
    can only react to a CLOSE — "closed below the level, therefore flat from
    the next open". A real protective order fills when price TOUCHES it, which
    is a within-bar event this contract cannot express. Setting
    ``stop_rule="chandelier"`` moves the exit into the engine, where it is
    tested against each bar's own low and respects the TWSE ±10% band (a
    limit-down lock has no counterparty, so the stop does not fill that day).

Equal weight across whichever names are currently in a breakout, so the book
is never more than 100% invested.
"""

from __future__ import annotations

import pandas as pd


class SignalEngine:
    """Donchian breakout entries; exits are the engine's protective stop."""

    def __init__(self, entry_window: int = 55, trend_window: int = 200) -> None:
        """
        Args:
            entry_window: Breakout lookback in bars.
            trend_window: Long moving-average filter; entries are refused
                below it so the stop is not fed counter-trend trades.
        """
        self.entry_window = entry_window
        self.trend_window = trend_window

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        """Target weights per symbol.

        Args:
            data_map: symbol -> OHLCV frame.

        Returns:
            symbol -> target weight series in [0, 1].
        """
        raw: dict[str, pd.Series] = {}

        for symbol, df in data_map.items():
            close = df["close"]
            # shift(1): the breakout level and the trend filter must be known
            # before the bar they gate, or the entry is lookahead.
            breakout_level = close.rolling(self.entry_window).max().shift(1)
            trend = close.rolling(self.trend_window).mean().shift(1)

            wants_in = (close > breakout_level) & (close > trend)
            # Hold until the engine's stop takes the position out: once a
            # breakout fires, stay long while price holds above the trend
            # filter. The stop, not this signal, decides the exit.
            fired = wants_in.astype(float).where(wants_in).ffill().fillna(0.0)
            raw[symbol] = ((fired > 0) & (close > trend)).astype(float)

        if not raw:
            return {}

        # Equal weight among the names currently signalled, so the book is
        # never geared beyond 100% however many fire at once.
        active = pd.DataFrame(raw).fillna(0.0)
        count = active.sum(axis=1).replace(0, float("nan"))
        weights = active.div(count, axis=0).fillna(0.0)
        return {symbol: weights[symbol] for symbol in weights.columns}
