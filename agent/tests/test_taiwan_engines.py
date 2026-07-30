"""Tests for the Taiwan market: TAIFEX futures, TWSE/TPEx equities, FinMind loader.

Validates:
  - Contract multipliers (TXF=200, MXF=50, TMF=10 NT$/point)
  - Absolute NT$ initial margin per contract (TAIFEX publishes amounts, not rates)
  - Cost stack: 期交稅 on notional + per-lot commission, charged both sides
  - Cash equity: 1,000-share board lots, 證交稅 on the sell side only, minimum fee
  - Price limits ±10% for both engines
  - Symbol -> market routing, including the collisions worth guarding:
    ``TXF2608`` must not fall through to global futures, and CFFEX ``TF2406``
    must not be stolen by TAIFEX
  - FinMind dataset/data_id resolution (no network)
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engines._market_hooks import _detect_market
from backtest.engines.taiwan_equity import TaiwanEquityEngine
from backtest.engines.taiwan_futures import (
    TaiwanFuturesEngine,
    _INITIAL_MARGIN,
    _MULTIPLIER,
    _extract_product,
)
from backtest.loaders.finmind_loader import _resolve_dataset
from backtest.loaders.registry import VALID_SOURCES
from backtest.models import Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(close: float = 20000.0, pre_close: float | None = None) -> pd.Series:
    d: dict = {"close": close, "open": close}
    if pre_close is not None:
        d["pre_close"] = pre_close
    return pd.Series(d)


def _futures_engine(**overrides) -> TaiwanFuturesEngine:
    config = {"initial_cash": 1_000_000, "codes": ["TXF202608"]}
    config.update(overrides)
    return TaiwanFuturesEngine(config)


def _equity_engine(**overrides) -> TaiwanEquityEngine:
    config = {"initial_cash": 1_000_000, "codes": ["2330.TW"]}
    config.update(overrides)
    return TaiwanEquityEngine(config)


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------


class TestExtractProduct:
    @pytest.mark.parametrize(
        "symbol, expected",
        [
            ("TXF202608.TAIFEX", "TXF"),
            ("TXF2608", "TXF"),
            ("MXF2608", "MXF"),
            ("TMF", "TMF"),
            ("txf2608", "TXF"),
        ],
    )
    def test_extract(self, symbol: str, expected: str) -> None:
        assert _extract_product(symbol) == expected


# ---------------------------------------------------------------------------
# Contract mechanics
# ---------------------------------------------------------------------------


class TestContractMechanics:
    def test_multipliers(self) -> None:
        eng = _futures_engine()
        assert eng.get_contract_multiplier("TXF2608") == 200.0
        assert eng.get_contract_multiplier("MXF2608") == 50.0
        assert eng.get_contract_multiplier("TMF2608") == 10.0

    def test_one_point_on_one_lot(self) -> None:
        """大台 1 index point on 1 contract = NT$200."""
        eng = _futures_engine()
        pnl = eng._calc_pnl("TXF2608", direction=1, size=1,
                            entry_price=20000.0, exit_price=20001.0)
        assert pnl == pytest.approx(200.0)

    def test_small_contract_is_a_quarter_of_the_big_one(self) -> None:
        eng = _futures_engine()
        big = eng._calc_pnl("TXF2608", 1, 1, 20000.0, 20010.0)
        small = eng._calc_pnl("MXF2608", 1, 1, 20000.0, 20010.0)
        assert big == pytest.approx(small * 4)

    def test_unknown_product_defaults_to_small_contract(self) -> None:
        assert _futures_engine().get_contract_multiplier("ZZZ9999") == 50.0

    def test_margin_is_absolute_not_price_scaled(self) -> None:
        """TAIFEX charges a fixed NT$ margin per contract."""
        eng = _futures_engine()
        cheap = eng._calc_margin("TXF2608", size=2, price=15000.0, leverage=eng.default_leverage)
        rich = eng._calc_margin("TXF2608", size=2, price=25000.0, leverage=eng.default_leverage)
        assert cheap == rich == pytest.approx(2 * _INITIAL_MARGIN["TXF"])

    def test_margin_override(self) -> None:
        eng = _futures_engine(margin_override=100_000.0)
        assert eng._calc_margin("TXF2608", 3, 20000.0, eng.default_leverage) == pytest.approx(300_000.0)

    def test_unknown_product_falls_back_to_rate_margin(self) -> None:
        eng = _futures_engine()
        margin = eng._calc_margin("ZZZ9999", size=1, price=20000.0, leverage=20.0)
        assert margin == pytest.approx(20000.0 * 50.0 / 20.0)


# ---------------------------------------------------------------------------
# Futures costs
# ---------------------------------------------------------------------------


class TestFuturesCosts:
    def test_commission_is_per_lot_fee_plus_notional_tax(self) -> None:
        eng = _futures_engine()
        cost = eng.calc_commission_for_symbol("TXF2608", size=1, price=20000.0, is_open=True)
        expected = 20.0 + (20000.0 * 200.0) * 0.00002
        assert cost == pytest.approx(expected)

    def test_round_trip_matches_reference_cost(self) -> None:
        """Cross-check against the standalone reference implementation:
        1 TXF lot bought at 100 and sold at 110 costs
        ``2 * per-lot fee + (100 + 110) * 200 * tax``."""
        eng = _futures_engine()
        total = (
            eng.calc_commission_for_symbol("TXF2608", 1, 100.0, is_open=True)
            + eng.calc_commission_for_symbol("TXF2608", 1, 110.0, is_open=False)
        )
        assert total == pytest.approx(2 * 20.0 + (100.0 + 110.0) * 200.0 * 0.00002)

    def test_costs_are_symmetric_across_sides(self) -> None:
        eng = _futures_engine()
        opening = eng.calc_commission_for_symbol("TXF2608", 1, 20000.0, is_open=True)
        closing = eng.calc_commission_for_symbol("TXF2608", 1, 20000.0, is_open=False)
        assert opening == pytest.approx(closing)

    def test_tax_scales_with_multiplier(self) -> None:
        eng = _futures_engine()
        big = eng.calc_commission_for_symbol("TXF2608", 1, 20000.0, True) - 20.0
        small = eng.calc_commission_for_symbol("MXF2608", 1, 20000.0, True) - 20.0
        assert big == pytest.approx(small * 4)

    def test_configurable_rates(self) -> None:
        eng = _futures_engine(commission_per_lot=12.0, futures_tax_rate=0.0)
        assert eng.calc_commission_for_symbol("TXF2608", 2, 20000.0, True) == pytest.approx(24.0)

    def test_slippage_always_hurts(self) -> None:
        eng = _futures_engine(slippage=0.001)
        assert eng.apply_slippage(20000.0, 1) > 20000.0     # buying pays up
        assert eng.apply_slippage(20000.0, -1) < 20000.0    # selling receives less


# ---------------------------------------------------------------------------
# Futures market rules
# ---------------------------------------------------------------------------


class TestFuturesRules:
    def test_whole_contracts_only(self) -> None:
        eng = _futures_engine()
        assert eng.round_size(3.9, 20000.0) == 3.0
        assert eng.round_size(0.4, 20000.0) == 0.0

    def test_both_directions_allowed(self) -> None:
        eng = _futures_engine()
        bar = _make_bar(20000.0, pre_close=20000.0)
        assert eng.can_execute("TXF2608", 1, bar)
        assert eng.can_execute("TXF2608", -1, bar)

    def test_limit_up_blocks_new_longs(self) -> None:
        eng = _futures_engine()
        bar = _make_bar(22000.0, pre_close=20000.0)   # +10%
        assert not eng.can_execute("TXF2608", 1, bar)

    def test_limit_down_blocks_new_shorts(self) -> None:
        eng = _futures_engine()
        bar = _make_bar(18000.0, pre_close=20000.0)   # -10%
        assert not eng.can_execute("TXF2608", -1, bar)

    def test_cannot_close_long_at_limit_down(self) -> None:
        eng = _futures_engine()
        eng.positions["TXF2608"] = Position(
            symbol="TXF2608", direction=1, size=1, entry_price=20000.0,
            entry_time=pd.Timestamp("2026-01-05"),
        )
        assert not eng.can_execute("TXF2608", 0, _make_bar(18000.0, pre_close=20000.0))

    def test_price_limit_can_be_disabled(self) -> None:
        eng = _futures_engine(price_limit=0)
        assert eng.can_execute("TXF2608", 1, _make_bar(22000.0, pre_close=20000.0))


# ---------------------------------------------------------------------------
# Cash equity
# ---------------------------------------------------------------------------


class TestTaiwanEquity:
    def test_board_lot_rounding(self) -> None:
        eng = _equity_engine()
        assert eng.round_size(2500, 600.0) == 2000.0   # floors to 2 lots
        assert eng.round_size(999, 600.0) == 0.0       # below one lot
        assert eng.round_size(1000, 600.0) == 1000.0

    def test_odd_lot_mode(self) -> None:
        eng = _equity_engine(lot_size=1)
        assert eng.round_size(137, 600.0) == 137.0

    def test_transaction_tax_is_sell_side_only(self) -> None:
        eng = _equity_engine()
        notional = 1000 * 600.0
        buy = eng.calc_commission(1000, 600.0, 1, is_open=True)
        sell = eng.calc_commission(1000, 600.0, 0, is_open=False)
        assert sell - buy == pytest.approx(notional * 0.003)

    def test_brokerage_matches_discounted_rate(self) -> None:
        eng = _equity_engine()
        notional = 1000 * 600.0
        assert eng.calc_commission(1000, 600.0, 1, is_open=True) == pytest.approx(
            notional * 0.001425 * 0.6
        )

    def test_minimum_commission_applies_to_tiny_orders(self) -> None:
        eng = _equity_engine()
        # 1 share at NT$10: percentage fee would be well under the NT$20 floor
        assert eng.calc_commission(1, 10.0, 1, is_open=True) == pytest.approx(20.0)

    def test_day_trade_tax_knob(self) -> None:
        eng = _equity_engine(tw_tax=0.0015)
        notional = 1000 * 600.0
        buy = eng.calc_commission(1000, 600.0, 1, is_open=True)
        sell = eng.calc_commission(1000, 600.0, 0, is_open=False)
        assert sell - buy == pytest.approx(notional * 0.0015)

    def test_short_blocked_by_default_and_enabled_by_config(self) -> None:
        bar = _make_bar(600.0, pre_close=600.0)
        assert not _equity_engine().can_execute("2330.TW", -1, bar)
        assert _equity_engine(allow_short=True).can_execute("2330.TW", -1, bar)

    def test_same_bar_sell_allowed(self) -> None:
        """當日沖銷 is legal, unlike a T+1 delivery market."""
        eng = _equity_engine()
        eng.positions["2330.TW"] = Position(
            symbol="2330.TW", direction=1, size=1000, entry_price=600.0,
            entry_time=pd.Timestamp("2026-01-05"),
        )
        bar = _make_bar(605.0, pre_close=600.0)
        assert eng.can_execute("2330.TW", 0, bar)

    def test_limit_up_blocks_buy_limit_down_blocks_sell(self) -> None:
        eng = _equity_engine()
        assert not eng.can_execute("2330.TW", 1, _make_bar(660.0, pre_close=600.0))
        assert not eng.can_execute("2330.TW", 0, _make_bar(540.0, pre_close=600.0))

    def test_no_leverage(self) -> None:
        assert _equity_engine().default_leverage == 1.0


# ---------------------------------------------------------------------------
# Symbol routing
# ---------------------------------------------------------------------------


class TestMarketDetection:
    @pytest.mark.parametrize(
        "symbol, expected",
        [
            ("2330.TW", "tw_equity"),
            ("0050.TW", "tw_equity"),
            ("00632R.TW", "tw_equity"),
            ("6488.TWO", "tw_equity"),
            ("TXF", "tw_futures"),
            ("TXF2608", "tw_futures"),
            ("MXF2608", "tw_futures"),
            ("TMF2608", "tw_futures"),
            ("TE2608.TAIFEX", "tw_futures"),
        ],
    )
    def test_taiwan_symbols(self, symbol: str, expected: str) -> None:
        assert _detect_market(symbol) == expected

    @pytest.mark.parametrize(
        "symbol, expected",
        [
            ("TF2406.CFFEX", "futures"),    # CFFEX bond futures, not TAIFEX
            ("IF2406.CFFEX", "futures"),
            ("ES2503", "futures"),          # global futures still routed away
            ("600519.SH", "a_share"),
            ("RELIANCE.NS", "india_equity"),
        ],
    )
    def test_no_collateral_damage(self, symbol: str, expected: str) -> None:
        assert _detect_market(symbol) == expected


# ---------------------------------------------------------------------------
# FinMind loader (offline)
# ---------------------------------------------------------------------------


class TestFinMindLoader:
    def test_registered_as_valid_source(self) -> None:
        assert "finmind" in VALID_SOURCES

    @pytest.mark.parametrize(
        "code, dataset, data_id, derivative",
        [
            ("2330", "TaiwanStockPrice", "2330", False),
            ("2330.TW", "TaiwanStockPrice", "2330", False),
            ("TXF", "TaiwanFuturesDaily", "TX", True),
            ("TXF202608", "TaiwanFuturesDaily", "TX", True),
            ("MXF2608", "TaiwanFuturesDaily", "MTX", True),
            ("TXO", "TaiwanOptionDaily", "TXO", True),
        ],
    )
    def test_dataset_resolution(
        self, code: str, dataset: str, data_id: str, derivative: bool
    ) -> None:
        assert _resolve_dataset(code) == (dataset, data_id, derivative)

    def test_rejects_intraday_interval(self) -> None:
        from backtest.loaders.finmind_loader import DataLoader

        with pytest.raises(ValueError, match="daily"):
            DataLoader().fetch(["2330"], "2026-01-01", "2026-01-31", interval="5m")


# ---------------------------------------------------------------------------
# Multiplier / margin tables stay in sync
# ---------------------------------------------------------------------------


def test_every_margin_entry_has_a_multiplier() -> None:
    assert set(_INITIAL_MARGIN) <= set(_MULTIPLIER)
