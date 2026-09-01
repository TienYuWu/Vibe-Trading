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

from backtest import taifex_margins
from backtest.engines._market_hooks import _detect_market
from backtest.taifex_margins import FALLBACK_INITIAL_MARGIN, parse_margin_rows

_INITIAL_MARGIN = FALLBACK_INITIAL_MARGIN
from backtest.engines.taiwan_equity import (
    TaiwanEquityEngine,
    twse_round_down,
    twse_round_up,
    twse_tick_size,
)
from backtest.engines.taiwan_futures import (
    TaiwanFuturesEngine,
    _MULTIPLIER,
    _TICK,
    _extract_product,
)
from backtest.loaders.finmind_loader import _resolve_dataset
from backtest.loaders.registry import VALID_SOURCES
from backtest.models import Position


@pytest.fixture(autouse=True)
def _pin_margins(monkeypatch):
    """Pin the bundled margin snapshot so tests never touch the TAIFEX API."""
    monkeypatch.setenv(taifex_margins.AUTOUPDATE_ENV, "0")
    taifex_margins.reset_cache()
    yield
    taifex_margins.reset_cache()


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

    def test_unknown_product_raises_instead_of_guessing(self) -> None:
        """A guessed multiplier would silently mis-price PnL, margin and tax."""
        with pytest.raises(ValueError, match="no contract multiplier"):
            _futures_engine().get_contract_multiplier("ZZZ9999")

    def test_margin_is_absolute_not_price_scaled(self) -> None:
        """TAIFEX charges a fixed NT$ margin per contract."""
        eng = _futures_engine()
        cheap = eng._calc_margin("TXF2608", size=2, price=15000.0, leverage=eng.default_leverage)
        rich = eng._calc_margin("TXF2608", size=2, price=25000.0, leverage=eng.default_leverage)
        assert cheap == rich == pytest.approx(2 * _INITIAL_MARGIN["TXF"])

    def test_margin_override(self) -> None:
        eng = _futures_engine(margin_override=100_000.0)
        assert eng._calc_margin("TXF2608", 3, 20000.0, eng.default_leverage) == pytest.approx(300_000.0)

    def test_rate_margin_fallback_for_listed_product_without_a_margin(self) -> None:
        """A product with a multiplier but no exchange margin sizes off leverage."""
        eng = _futures_engine()
        monkey = dict(taifex_margins.FALLBACK_INITIAL_MARGIN)
        monkey.pop("TE", None)
        taifex_margins._cache = monkey
        margin = eng._calc_margin("TE2608.TAIFEX", size=1, price=1100.0, leverage=20.0)
        assert margin == pytest.approx(1100.0 * _MULTIPLIER["TE"] / 20.0)


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

    def test_commission_without_active_symbol_raises(self) -> None:
        """BaseEngine sets _active_symbol; reaching here without it must not
        silently price the tax off a guessed multiplier."""
        eng = _futures_engine()
        eng._active_symbol = ""
        with pytest.raises(ValueError, match="_active_symbol"):
            eng.calc_commission(1, 45000.0, 1, is_open=True)

    def test_commission_uses_active_symbol_when_set(self) -> None:
        eng = _futures_engine()
        eng._active_symbol = "TXF2608"
        assert eng.calc_commission(1, 45000.0, 1, is_open=True) == pytest.approx(
            eng.calc_commission_for_symbol("TXF2608", 1, 45000.0, is_open=True)
        )

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


class TestTickGrid:
    """Fills and band edges must be prices the exchange could have printed."""

    @pytest.mark.parametrize(
        "price, tick",
        [(9.99, 0.01), (10.0, 0.05), (49.95, 0.05), (50.0, 0.10),
         (99.9, 0.10), (100.0, 0.50), (499.5, 0.50), (500.0, 1.00),
         (999.0, 1.00), (1000.0, 5.00), (2500.0, 5.00)],
    )
    def test_stock_tick_bands(self, price: float, tick: float) -> None:
        assert twse_tick_size("2330.TW", price) == tick

    @pytest.mark.parametrize("price, tick", [(0.5, 0.01), (49.99, 0.01), (50.0, 0.05), (200.0, 0.05)])
    def test_etf_uses_its_own_finer_table(self, price: float, tick: float) -> None:
        """ETF codes (00xx) run a two-step table, not the stock one."""
        assert twse_tick_size("0050.TW", price) == tick
        assert twse_tick_size("00632R.TW", price) == tick

    def test_rounding_directions(self) -> None:
        assert twse_round_down("2330.TW", 601.4) == 601.0    # 500-1000 -> 1.0 tick
        assert twse_round_up("2330.TW", 601.4) == 602.0
        assert twse_round_down("2330.TW", 87.34) == 87.30    # 50-100 -> 0.1 tick
        assert twse_round_up("2330.TW", 87.34) == 87.40

    def test_equity_fill_is_snapped_away_from_the_trader(self) -> None:
        eng = _equity_engine(slippage=0.001)
        eng._active_symbol = "2330.TW"
        buy = eng.apply_slippage(600.0, 1)     # 600.6 raw -> next tick up
        sell = eng.apply_slippage(600.0, -1)   # 599.4 raw -> next tick down
        assert buy == 601.0
        assert sell == 599.0
        assert buy > 600.0 > sell, "quantisation must never flatter the fill"

    def test_equity_band_edges_land_on_the_grid_inside_the_band(self) -> None:
        eng = _equity_engine()
        bar = _make_bar(600.0, pre_close=600.0)
        lower, upper = eng.limit_band("2330.TW", bar, 0.10)
        assert (lower, upper) == (540.0, 660.0)
        # 87.5 x 1.1 = 96.25, which is off the 0.1 grid: it truncates inward.
        lower, upper = eng.limit_band("2330.TW", _make_bar(87.5, pre_close=87.5), 0.10)
        assert upper == 96.2 and lower == 78.8
        assert upper < 87.5 * 1.10 and lower > 87.5 * 0.90

    @pytest.mark.parametrize(
        "symbol, tick", [("TXF2608", 1.0), ("MXF2608", 1.0), ("TMF2608", 1.0),
                         ("TE2608.TAIFEX", 0.05), ("TF2608.TAIFEX", 0.2)],
    )
    def test_futures_product_ticks(self, symbol: str, tick: float) -> None:
        assert _TICK[_extract_product(symbol)] == tick

    def test_futures_fill_is_snapped_away_from_the_trader(self) -> None:
        eng = _futures_engine(slippage=0.001)
        eng._active_symbol = "TXF2608"
        assert eng.apply_slippage(20000.0, 1) == 20020.0    # whole points
        assert eng.apply_slippage(20000.0, -1) == 19980.0

    def test_futures_band_edges_land_on_the_grid(self) -> None:
        eng = _futures_engine()
        # 20005 x 1.1 = 22005.5 -> truncates to 22005 (inside the band).
        lower, upper = eng.limit_band("TXF2608", _make_bar(20005.0, pre_close=20005.0), 0.10)
        assert upper == 22005.0 and lower == 18005.0
        assert upper < 20005.0 * 1.10 and lower > 20005.0 * 0.90

    def test_unknown_product_keeps_the_raw_price(self) -> None:
        """No invented grid for a contract whose tick we do not know."""
        eng = _futures_engine(slippage=0.001)
        eng._active_symbol = "ZZZ2608.TAIFEX"
        assert eng.apply_slippage(20000.0, 1) == pytest.approx(20020.0)
        assert eng.limit_band("ZZZ2608.TAIFEX", _make_bar(20005.0, pre_close=20005.0), 0.10) == (
            pytest.approx(20005.0 * 0.9), pytest.approx(20005.0 * 1.1)
        )


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

    def test_equity_rows_keep_nt_turnover(self) -> None:
        """``Trading_money`` becomes ``amount`` — raw NT$, no 千元 scaling."""
        from backtest.loaders.finmind_loader import _normalize_equity

        frame = _normalize_equity([
            {"date": "2024-01-02", "open": 590.0, "max": 593.0, "min": 589.0,
             "close": 593.0, "Trading_Volume": 27997826, "Trading_money": 16549619798},
        ])
        assert frame["amount"].iloc[0] == 16549619798
        assert frame["volume"].iloc[0] == 27997826

    def test_derivative_rows_carry_the_previous_settlement(self) -> None:
        """TAIFEX bands come off the prior settlement, so the loader supplies it.

        Without ``pre_settle`` the engine's price-limit check silently falls
        back to the previous close, which is not the rule the exchange applies.
        """
        from backtest.loaders.finmind_loader import _normalize_derivative

        frame = _normalize_derivative([
            {"date": "2024-01-02", "open": 17838.0, "max": 17920.0, "min": 17751.0,
             "close": 17798.0, "volume": 82827, "settlement_price": 17800.0},
            {"date": "2024-01-03", "open": 17658.0, "max": 17667.0, "min": 17507.0,
             "close": 17546.0, "volume": 97851, "settlement_price": 17545.0},
        ])
        assert pd.isna(frame["pre_settle"].iloc[0])   # nothing precedes bar 1
        assert frame["pre_settle"].iloc[1] == 17800.0
        assert frame["settle"].iloc[1] == 17545.0

    def test_zero_settlement_never_becomes_a_base_price(self) -> None:
        """TAIFEX publishes 0 settlements on thin days; a 0 band base is absurd."""
        from backtest.loaders.finmind_loader import _normalize_derivative

        frame = _normalize_derivative([
            {"date": "2024-01-02", "open": 100.0, "max": 101.0, "min": 99.0,
             "close": 100.0, "volume": 10, "settlement_price": 0.0},
            {"date": "2024-01-03", "open": 100.0, "max": 101.0, "min": 99.0,
             "close": 100.0, "volume": 10, "settlement_price": 101.0},
        ])
        assert pd.isna(frame["pre_settle"].iloc[1])


class TestTwse50BenchUniverse:
    def test_registered_as_a_bench_universe(self) -> None:
        from src.tools.alpha_bench_tool import _UNIVERSE_TAG

        assert _UNIVERSE_TAG["twse50"] == "equity_tw"

    def test_cli_and_rest_offer_it(self) -> None:
        """A universe with a panel must be reachable from both surfaces."""
        from src.api.alpha_routes import _BENCH_UNIVERSES
        from src.factors.cli_handlers import _UNIVERSE_CHOICES, _LIST_UNIVERSE_ALIASES

        assert "twse50" in _BENCH_UNIVERSES
        assert "twse50" in _UNIVERSE_CHOICES
        assert _LIST_UNIVERSE_ALIASES["twse50"] == "equity_tw"

    def test_constituents_are_unique_and_full_size(self) -> None:
        from src.tools.alpha_bench_tool import _TWSE50_CODES

        assert len(_TWSE50_CODES) == 50
        assert len(set(_TWSE50_CODES)) == 50
        assert all(c.isdigit() and len(c) == 4 for c in _TWSE50_CODES)

    def test_every_bench_universe_has_a_loader_branch(self) -> None:
        """_UNIVERSE_TAG and the dispatch in _load_universe_panel must agree."""
        import inspect

        from src.tools import alpha_bench_tool

        source = inspect.getsource(alpha_bench_tool._load_universe_panel)
        for name in alpha_bench_tool._UNIVERSE_TAG:
            assert f'"{name}"' in source, f"{name} has no branch in _load_universe_panel"


# ---------------------------------------------------------------------------
# Multiplier / margin tables stay in sync
# ---------------------------------------------------------------------------


def test_every_margin_entry_has_a_multiplier() -> None:
    assert set(_INITIAL_MARGIN) <= set(_MULTIPLIER)


def test_margins_scale_with_contract_size() -> None:
    """小台 = 1/4 大台, 微台 = 1/20 大台 — margins must track the multipliers."""
    big = _INITIAL_MARGIN["TXF"] / _MULTIPLIER["TXF"]
    for product in ("MXF", "TMF"):
        per_point = _INITIAL_MARGIN[product] / _MULTIPLIER[product]
        assert per_point == pytest.approx(big, rel=0.02)


class TestMarginAutoUpdate:
    """The live-table path: parsing, gating, and fallback."""

    def test_parses_official_rows(self) -> None:
        rows = [
            {"Contract": "臺股期貨", "ClearingMargin": "471000",
             "MaintenanceMargin": "488000", "InitialMargin": "636000", "Date": "20260729"},
            {"Contract": "小型臺指", "ClearingMargin": "117750",
             "MaintenanceMargin": "122000", "InitialMargin": "159000", "Date": "20260729"},
            {"Contract": "臺指選擇權風險保證金(A)值", "ClearingMargin": "125000",
             "MaintenanceMargin": "130000", "InitialMargin": "169000", "Date": "20260729"},
        ]
        parsed = parse_margin_rows(rows)
        assert parsed == {"TXF": 636000.0, "MXF": 159000.0}   # option A-value ignored

    def test_skips_unparseable_and_zero_rows(self) -> None:
        rows = [
            {"Contract": "臺股期貨", "InitialMargin": "636,000"},   # thousands separator
            {"Contract": "電子期貨", "InitialMargin": ""},          # blank
            {"Contract": "金融期貨", "InitialMargin": "0"},         # zero
            {"Contract": "未知新商品", "InitialMargin": "12345"},    # unmapped
        ]
        assert parse_margin_rows(rows) == {"TXF": 636000.0}

    def test_env_gate_pins_the_snapshot(self, monkeypatch) -> None:
        monkeypatch.setenv(taifex_margins.AUTOUPDATE_ENV, "0")
        taifex_margins.reset_cache()
        assert taifex_margins.get_initial_margins() == FALLBACK_INITIAL_MARGIN

    def test_falls_back_when_the_api_fails(self, monkeypatch) -> None:
        monkeypatch.setenv(taifex_margins.AUTOUPDATE_ENV, "1")
        monkeypatch.setattr(taifex_margins, "_read_cache", lambda: None)

        def _boom():
            raise RuntimeError("TAIFEX unreachable")

        monkeypatch.setattr(taifex_margins, "fetch_initial_margins", _boom)
        taifex_margins.reset_cache()
        assert taifex_margins.get_initial_margins() == FALLBACK_INITIAL_MARGIN

    def test_live_values_win_over_the_snapshot(self, monkeypatch) -> None:
        monkeypatch.setenv(taifex_margins.AUTOUPDATE_ENV, "1")
        monkeypatch.setattr(taifex_margins, "_read_cache", lambda: None)
        monkeypatch.setattr(taifex_margins, "_write_cache", lambda *a, **k: None)
        monkeypatch.setattr(
            taifex_margins, "fetch_initial_margins",
            lambda: ({"TXF": 700000.0}, "20260901"),
        )
        taifex_margins.reset_cache()
        margins = taifex_margins.get_initial_margins()
        assert margins["TXF"] == 700000.0                       # refreshed
        assert margins["MXF"] == FALLBACK_INITIAL_MARGIN["MXF"]  # untouched keys survive


# Only the TAIEX-tracking contracts; TE (電子指數 ~1,100) and TF (金融指數 ~2,300)
# track different underlyings, so REFERENCE_INDEX_LEVEL says nothing about them.
_TAIEX_PRODUCTS = ("TXF", "MXF", "TMF")


@pytest.mark.parametrize("product", _TAIEX_PRODUCTS)
def test_implied_margin_rate_is_plausible(product: str) -> None:
    """Guard against a stale TAIEX margin snapshot.

    TAIFEX rescales margins as the index moves, so a pinned amount rots
    silently: the TXF figure that was right near a 15,000 index implies a ~2%
    margin at 45,000, which no exchange would set. Requiring 3%-20% of notional
    at the reference index catches that drift instead of shipping wrong margin.
    """
    from backtest.engines.taiwan_futures import REFERENCE_INDEX_LEVEL

    notional = REFERENCE_INDEX_LEVEL * _MULTIPLIER[product]
    rate = _INITIAL_MARGIN[product] / notional
    assert 0.03 <= rate <= 0.20, f"{product} implies a {rate:.1%} margin rate"


# ---------------------------------------------------------------------------
# Bare Taiwan code -> symbol resolution
# ---------------------------------------------------------------------------


class TestTaiwanSymbolSearch:
    """A bare ``2330`` must reach TSMC in Taipei, not Hong Kong's ``02330.HK``.

    Before this source existed the query returned only Hong Kong and mainland
    candidates, so an agent citing price evidence for a Taiwan backtest read a
    different company's HKD prices and the grounding gate refused the answer.
    """

    @pytest.mark.parametrize(
        "code", ["2330", "0050", "6488", "00632R", "2454", "1101"]
    )
    def test_bare_taiwan_codes_are_recognised(self, code: str) -> None:
        from src.tools.symbol_search_tool import _is_bare_tw_code

        assert _is_bare_tw_code(code)

    @pytest.mark.parametrize(
        "text",
        [
            "AAPL",          # bare US ticker
            "02330",         # Hong Kong prints a leading zero
            "600519",        # six-digit A-share
            "2330.TW",       # already suffixed; the search path, not this one
            "",
            "TSMC",
        ],
    )
    def test_non_taiwan_shapes_are_left_alone(self, text: str) -> None:
        """The regex only decides whether to ASK FinMind; over-matching would
        send every numeric query to a Taiwan-only source."""
        from src.tools.symbol_search_tool import _is_bare_tw_code

        assert not _is_bare_tw_code(text)

    @pytest.mark.parametrize(
        "symbol, expected",
        [("2330.TW", True), ("6488.TWO", True), ("2330", False),
         ("02330.HK", False), ("AAPL.US", False)],
    )
    def test_taiwan_venue_detection(self, symbol: str, expected: bool) -> None:
        from src.tools.symbol_search_tool import _is_tw_symbol

        assert _is_tw_symbol(symbol) is expected

    def test_yahoo_labels_taiwan_as_tw_not_global(self) -> None:
        """Both sources must agree on the label, or one listing merges under two
        market names depending on which source answered first."""
        from src.tools.symbol_search_tool import _from_yahoo_symbol

        assert _from_yahoo_symbol("2330.TW", {"quoteType": "EQUITY"}) == ("2330.TW", "tw")
        assert _from_yahoo_symbol("6488.TWO", {"quoteType": "EQUITY"}) == ("6488.TWO", "tw")

    def test_listing_rows_map_venue_and_type(self, monkeypatch) -> None:
        """TWSE -> .TW, TPEx -> .TWO; an unmapped venue is skipped, not guessed."""
        from backtest.loaders import finmind_loader

        finmind_loader.reset_listing_cache()
        monkeypatch.setattr(
            finmind_loader,
            "_load_listing_table",
            lambda: {
                "2330": [{"code": "2330", "suffix": "TW", "name": "台積電", "type": "equity"}],
                "6488": [{"code": "6488", "suffix": "TWO", "name": "廣明", "type": "equity"}],
                "0050": [{"code": "0050", "suffix": "TW", "name": "元大台灣50", "type": "etf"}],
            },
        )
        assert finmind_loader.fetch_listing("2330")[0]["suffix"] == "TW"
        assert finmind_loader.fetch_listing("6488")[0]["suffix"] == "TWO"
        assert finmind_loader.fetch_listing("0050")[0]["type"] == "etf"
        assert finmind_loader.fetch_listing("9999") == []

    def test_search_skips_a_non_taiwan_query_without_erroring(self) -> None:
        """A skip and a failure are not the same to the grounding ledger: a
        source recorded as failed turns "not listed here" into a blocking
        ambiguity."""
        from src.tools.symbol_search_tool import _SKIPPED, _search_finmind

        candidates, status = _search_finmind("AAPL")
        assert candidates == []
        assert status.startswith(_SKIPPED)
