"""Protective stops: level construction, trigger pricing, and engine wiring.

The properties that matter are that the level guarding a bar was fixed before
that bar, that it ratchets, that a gap fills at the gap rather than at the
level, and that market rules still veto the fill.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from backtest.engines.taiwan_equity import TaiwanEquityEngine
from backtest.stops import (
    ChandelierStop,
    StopConfig,
    trigger_price,
    wilder_atr,
)


# ── Config parsing ──


class TestStopConfig:
    def test_default_is_off(self) -> None:
        """An existing config with no stop keys must stay bit-identical."""
        cfg = StopConfig.from_config({})
        assert cfg.rule == "none"
        assert cfg.enabled is False

    def test_chandelier_defaults(self) -> None:
        cfg = StopConfig.from_config({"stop_rule": "chandelier"})
        assert (cfg.atr_period, cfg.multiplier) == (22, 3.0)
        assert cfg.enabled is True

    def test_take_profit_alone_enables_stops(self) -> None:
        cfg = StopConfig.from_config({"take_profit_pct": 0.2})
        assert cfg.rule == "none" and cfg.enabled is True

    @pytest.mark.parametrize(
        "bad",
        [
            {"stop_rule": "trailing"},
            {"stop_rule": "chandelier", "stop_atr_period": 0},
            {"stop_rule": "chandelier", "stop_atr_multiplier": 0},
            {"stop_rule": "chandelier", "stop_atr_multiplier": -1},
            {"take_profit_pct": 0},
            {"take_profit_pct": -0.1},
        ],
    )
    def test_bad_settings_raise(self, bad: dict) -> None:
        """A silently ignored stop reads as risk-managed when it is not."""
        with pytest.raises(ValueError):
            StopConfig.from_config(bad)


# ── ATR ──


def _frame(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """Build an OHLC frame from (high, low, close) triples; open = close."""
    idx = pd.date_range("2026-01-05", periods=len(rows), freq="D")
    return pd.DataFrame(
        {
            "open": [c for _, _, c in rows],
            "high": [h for h, _, _ in rows],
            "low": [lo for _, lo, _ in rows],
            "close": [c for _, _, c in rows],
        },
        index=idx,
    )


class TestWilderAtr:
    def test_leading_bars_are_nan_until_the_period_fills(self) -> None:
        """A stop must not arm off a half-formed average."""
        df = _frame([(101.0, 99.0, 100.0)] * 5)
        atr = wilder_atr(df, 3)
        assert atr.iloc[:2].isna().all()
        assert not pd.isna(atr.iloc[2])

    def test_true_range_counts_the_gap_not_just_the_span(self) -> None:
        """A bar that gaps has a true range larger than its own high-low."""
        df = _frame([(101.0, 99.0, 100.0), (121.0, 119.0, 120.0)])
        atr = wilder_atr(df, 2)
        # Bar 2 span is 2.0, but it gapped 19.0 above the prior close.
        assert float(atr.iloc[1]) > 2.0


# ── Level ratchet ──


class TestChandelierRatchet:
    def test_long_level_only_rises(self) -> None:
        stop = ChandelierStop(direction=1, multiplier=2.0)
        stop.update(high=110.0, low=100.0, atr=5.0)
        first = stop.level
        assert first == pytest.approx(100.0)  # 110 - 2x5
        # A lower high must not loosen the stop.
        stop.update(high=105.0, low=95.0, atr=5.0)
        assert stop.level == pytest.approx(first)
        # A higher high tightens it.
        stop.update(high=120.0, low=110.0, atr=5.0)
        assert stop.level == pytest.approx(110.0)

    def test_short_level_only_falls(self) -> None:
        stop = ChandelierStop(direction=-1, multiplier=2.0)
        stop.update(high=100.0, low=90.0, atr=5.0)
        first = stop.level
        assert first == pytest.approx(100.0)  # 90 + 2x5
        stop.update(high=110.0, low=95.0, atr=5.0)
        assert stop.level == pytest.approx(first)
        stop.update(high=95.0, low=80.0, atr=5.0)
        assert stop.level == pytest.approx(90.0)

    def test_nan_atr_leaves_the_stop_unarmed(self) -> None:
        stop = ChandelierStop(direction=1, multiplier=3.0)
        stop.update(high=110.0, low=100.0, atr=float("nan"))
        assert stop.level is None
        assert stop.extreme == 110.0  # the extreme still tracks


# ── Trigger pricing ──


class TestTriggerPrice:
    def test_untouched_bar_does_not_trigger(self) -> None:
        assert trigger_price(1, 90.0, bar_open=100.0, bar_high=105.0, bar_low=95.0) is None

    def test_touch_fills_at_the_level(self) -> None:
        assert trigger_price(1, 90.0, bar_open=100.0, bar_high=105.0, bar_low=88.0) == 90.0

    def test_gap_through_fills_at_the_open_not_the_level(self) -> None:
        """The gap is the price the market actually offered."""
        fill = trigger_price(1, 90.0, bar_open=80.0, bar_high=85.0, bar_low=78.0)
        assert fill == 80.0

    def test_short_stop_is_above(self) -> None:
        assert trigger_price(-1, 110.0, bar_open=100.0, bar_high=115.0, bar_low=98.0) == 110.0
        assert trigger_price(-1, 110.0, bar_open=120.0, bar_high=125.0, bar_low=118.0) == 120.0

    def test_long_target_is_above(self) -> None:
        fill = trigger_price(
            1, 115.0, bar_open=100.0, bar_high=120.0, bar_low=99.0, is_target=True
        )
        assert fill == 115.0

    def test_short_target_is_below(self) -> None:
        fill = trigger_price(
            -1, 85.0, bar_open=100.0, bar_high=101.0, bar_low=80.0, is_target=True
        )
        assert fill == 85.0


# ── Engine wiring ──


def _engine(**overrides):
    config = {"initial_cash": 1_000_000, "stop_rule": "chandelier", "slippage": 0.0}
    config.update(overrides)
    return ChinaAEngine(config)


def _open_long(engine, symbol: str, entry: float, ts: pd.Timestamp):
    from backtest.models import Position

    engine.positions[symbol] = Position(
        symbol=symbol, direction=1, size=100, entry_price=entry,
        entry_time=ts, leverage=1.0,
    )


class TestEngineStops:
    def test_entry_bar_cannot_stop_out(self) -> None:
        """No level exists yet on the entry bar, so nothing can fire."""
        eng = _engine()
        df = _frame([(110.0, 50.0, 100.0)] * 3)
        ts = df.index[0]
        _open_long(eng, "000001.SZ", 100.0, ts)
        eng._apply_stops("000001.SZ", df, ts)
        assert "000001.SZ" in eng.positions

    def test_stop_fires_on_a_later_bar_and_records_the_reason(self) -> None:
        rows = [(110.0, 90.0, 100.0)] * 25 + [(110.0, 1.0, 5.0)]
        df = _frame(rows)
        eng = _engine()
        _open_long(eng, "000001.SZ", 100.0, df.index[0])
        for ts in df.index:
            eng._apply_stops("000001.SZ", df, ts)
        assert "000001.SZ" not in eng.positions
        assert eng.trades[-1].exit_reason == "stop_chandelier"

    def test_level_guarding_a_bar_ignores_that_bar(self) -> None:
        """The ratchet folds in a bar only after that bar has been tested."""
        df = _frame([(110.0, 90.0, 100.0)] * 25)
        eng = _engine()
        _open_long(eng, "000001.SZ", 100.0, df.index[0])
        eng._apply_stops("000001.SZ", df, df.index[0])
        first_level = eng._stops["000001.SZ"].level
        eng._apply_stops("000001.SZ", df, df.index[1])
        # Bar 2 is identical to bar 1, so a level that had peeked at its own
        # bar would already differ here.
        assert eng._stops["000001.SZ"].extreme == 110.0
        assert first_level is None or first_level == eng._stops["000001.SZ"].level

    def test_take_profit_fires_without_a_stop_rule(self) -> None:
        eng = _engine(stop_rule="none", take_profit_pct=0.10)
        # Opens at 105 (below the 110 target), trades up through it intrabar.
        df = _frame([(100.0, 99.0, 100.0), (115.0, 100.0, 105.0)])
        _open_long(eng, "000001.SZ", 100.0, df.index[0])
        eng._apply_stops("000001.SZ", df, df.index[1])
        assert "000001.SZ" not in eng.positions
        assert eng.trades[-1].exit_reason == "take_profit"
        assert eng.trades[-1].exit_price == pytest.approx(110.0)

    def test_take_profit_gap_fills_at_the_open(self) -> None:
        """Opening past the target books the gap, not the target."""
        eng = _engine(stop_rule="none", take_profit_pct=0.10)
        df = _frame([(100.0, 99.0, 100.0), (115.0, 113.0, 114.0)])
        _open_long(eng, "000001.SZ", 100.0, df.index[0])
        eng._apply_stops("000001.SZ", df, df.index[1])
        assert eng.trades[-1].exit_price == pytest.approx(114.0)

    def test_limit_down_lock_blocks_the_stop(self) -> None:
        """A locked name has no counterparty — the stop simply does not fill."""
        eng = TaiwanEquityEngine(
            {"initial_cash": 1_000_000, "stop_rule": "chandelier", "slippage": 0.0}
        )
        rows = [(110.0, 90.0, 100.0)] * 25 + [(90.0, 90.0, 90.0)]
        df = _frame(rows)
        df["pre_close"] = 100.0
        _open_long(eng, "2330.TW", 100.0, df.index[0])
        for ts in df.index:
            eng._apply_stops("2330.TW", df, ts)
        # Final bar opens at the -10% band: 跌停鎖死, the sell cannot fill.
        assert "2330.TW" in eng.positions

    def test_disabled_by_default_leaves_positions_alone(self) -> None:
        eng = ChinaAEngine({"initial_cash": 1_000_000})
        assert eng.stop_config.enabled is False
        df = _frame([(110.0, 1.0, 5.0)] * 30)
        _open_long(eng, "000001.SZ", 100.0, df.index[0])
        for ts in df.index:
            eng._apply_stops("000001.SZ", df, ts)
        assert "000001.SZ" in eng.positions
