---
name: taiwan-market
description: Taiwan market backtesting — TWSE/TPEx cash equities and TAIFEX index futures via FinMind, with the exchange rules that change results (±10% band, 升降單位 tick grid, 證交稅/期交稅, board lots) and the engine-side Chandelier stop. Use when backtesting .TW / .TWO symbols or TXF/MXF/TMF contracts.
category: strategy
---
# Taiwan Market (TWSE / TPEx / TAIFEX)

## Purpose

Backtest Taiwan equities and index futures with the exchange mechanics modelled
rather than approximated. The rules below are the ones that move a result: a
strategy that looks profitable without the price band, the tick grid and the
0.3% sell-side tax will usually stop looking profitable with them.

## Symbols

| Form | Market | Example |
|---|---|---|
| `NNNN.TW` | TWSE listed | `2330.TW` |
| `NNNN.TWO` | TPEx listed | `6488.TWO` |
| `00NNNN.TW` | ETF (finer tick table) | `0050.TW`, `00632R.TW` |
| `TXF` / `MXF` / `TMF` + contract | TAIFEX index futures | `TXF2608` |
| `<PRODUCT>.TAIFEX` | TAIFEX, explicit suffix | `TE2608.TAIFEX` |

`TE` and `TF` **require** the `.TAIFEX` suffix — bare `TF2406` is CFFEX bond
futures and routes to the China futures engine.

## Data

`source: "finmind"`. Free, no API key, no extra dependency (plain HTTP).
`FINMIND_TOKEN` in `agent/.env` is optional and only raises the hourly request
cap. **Daily bars only** — a non-daily interval is refused so the runner falls
through to another source. Yahoo covers the cash equities as a fallback but
carries no TAIFEX derivatives at all.

## What the engines model

**`TaiwanEquityEngine`** (`tw_equity`)

| Rule | Behaviour | Config knob |
|---|---|---|
| 當日沖銷 | Same-day round trips allowed, no T+1 | — |
| 漲跌停 ±10% | From the previous close, judged at execution time against the prospective fill | `price_limit` |
| 升降單位 | Six-step stock table; ETF codes (`00…`) use their own finer two-step table. Fills snap away from the trader | — |
| 整股 | 1,000-share board lots | `lot_size` (`1` for 零股) |
| 手續費 | 0.1425% per side × discount, with a per-order minimum | `tw_brokerage` / `tw_discount` / `tw_min_commission` |
| 證交稅 | 0.3% sell side only | `tw_tax` (`0.0015` for a day-trading strategy) |
| 融券 | Off by default | `allow_short` |

**`TaiwanFuturesEngine`** (`tw_futures`)

| Rule | Behaviour |
|---|---|
| Direction | T+0, long and short |
| 漲跌停 ±10% | From the previous **settlement** (`pre_settle`), not the previous close |
| Tick | 1 point for TXF/MXF/TMF, 0.05 for TE, 0.2 for TF |
| 保證金 | TAIFEX's published absolute NT$ per contract, read from the exchange API at runtime — not a hardcoded constant, because the exchange rescales margins as the index moves |
| 期交稅 | On notional, both sides |

Unknown products fail loudly rather than guessing a contract multiplier.

## Stops belong in config.json, not in the signal engine

A `SignalEngine` emits target weights per bar, so any exit written there can
only react to a **close**: "closed below the level, therefore flat from the next
open". A protective order fills when price **touches** it — a within-bar event
the target-weight contract cannot express.

Put the exit in `config.json` and the engine tests it against each bar's own
high/low:

```json
{
  "codes": ["2330.TW", "2454.TW", "2317.TW"],
  "start_date": "2022-01-01",
  "end_date": "2024-12-31",
  "source": "finmind",
  "interval": "1D",
  "initial_cash": 1000000,
  "stop_rule": "chandelier",
  "stop_atr_period": 22,
  "stop_atr_multiplier": 3.0,
  "take_profit_pct": null
}
```

**Chandelier Exit** (Chuck LeBeau): for a long, the highest high reached since
entry minus `stop_atr_multiplier` ATRs. It ratchets — the level only moves in
the trade's favour, because a stop that can retreat from price is not
protection.

Interactions worth knowing before reading a Taiwan result:

- A name locked **limit-down has no counterparty**, so the stop does not fill
  that day and the position carries. This is the single biggest difference
  between a Taiwan stop and a US one, and it is modelled.
- A bar that **opens through** the level fills at the open, not the level.
- A position **cannot stop out on its entry bar** — no ATR and no extreme exist
  for it yet.
- Stop and target in the same bar: the **stop** is assumed, since OHLC cannot
  order the two events.

Stops are opt-in; omit `stop_rule` and the run has none. Exits are tagged
`stop_chandelier` / `take_profit` in `artifacts/trades.csv`.

## Example

`example_signal_engine.py` — 55-bar Donchian breakout with a 200-bar trend
filter, equal weight across whichever names are in a breakout, and **no exit
rule at all**: the position is handed to the engine's Chandelier stop. That
division of labour is the point of the example.

Verified against live FinMind data (2330.TW / 2454.TW / 2317.TW, 2022-2024,
config above):

| Metric | Value |
|---|---|
| Total return | +38.1% |
| Annual return | +11.3% |
| Sharpe | 1.00 |
| Max drawdown | -10.5% |
| Trades | 29 |
| Win rate | 34.5% |
| Profit factor | 2.49 |
| Exits by stop | 23 of 29 |

Buy-and-hold over the same window returned +64%, so this trend-following
example **underperforms its benchmark**. That is the expected shape for a
trailing-stop trend system in a strong bull market, not a defect — it is
included so the numbers are read honestly rather than as a promise.

## Factors

`equity_tw` is a valid factor universe: `alpha list --universe equity_tw`, the
REST `/alpha/list` filter, and factor computation on Taiwan panels all work.
Coverage is inherited from `equity_us` at the registry level, because FinMind
serves raw price and share volume with no Tushare 千元/手 scaling.

There is **no TWSE benchmark panel**, so `alpha bench --universe equity_tw` is
not offered — it would fail at universe load rather than produce a Taiwanese
benchmark. Evaluate a single factor by writing it into a `signal_engine.py` and
running a normal backtest.

## Signal Convention

`generate()` returns `symbol -> pd.Series` of target weights. Positive is long,
`0` is flat. The example keeps the book at or under 100% invested by splitting
weight equally among the names currently signalled.
