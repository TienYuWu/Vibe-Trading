---
name: Taiwan Close Scan
description: Post-close TWSE/TPEx scan — which watch-list names triggered an entry, which sit near a Chandelier stop, and which are locked at the daily band.
markets: [tw]
suggested_schedule: "30 14 * * 1-5"
suggested_timezone: Asia/Taipei
data_capabilities:
  - Daily price, volume and turnover history for TWSE and TPEx tickers
  - Daily price, volume, settlement and open interest for TAIFEX index futures
  - Backtest execution over a supplied signal engine and configuration
variables:
  watchlist: 2330.TW, 2454.TW, 2317.TW
  entry_window: "55"
  trend_window: "200"
  atr_multiplier: "3.0"
---

# Taiwan close scan

Report what the TWSE/TPEx session that just closed did to the watch list, and
nothing else. This is a state report, not a recommendation.

Resolve the current date from the run environment and confirm the session date
from the retrieved data itself. The exchange closes for national holidays and
for most of a week at Lunar New Year, so the newest available session is often
not today. State the date the report covers in the first line.

## Inputs

- Watch list: {{watchlist}}
- Breakout lookback: {{entry_window}} bars
- Trend filter: {{trend_window}}-bar moving average
- Chandelier stop: highest high since entry minus {{atr_multiplier}} ATR(22)

## Data to gather

For each watch-list symbol, over a window long enough to cover the trend filter
plus the ATR lookback:

1. Daily open, high, low, close and volume.
2. The previous close, which is what the ±10% band is measured from.
3. For a TAIFEX symbol, the previous settlement, which is what its band is
   measured from instead.

Taiwan daily bars come from a source that serves **daily granularity only**.
Do not report an intraday level, an intraday trigger time, or a current price:
the newest fact available is a completed daily bar.

## Method

Compute per symbol, from completed bars only:

- **Breakout state** — whether the close is above the highest close of the
  prior {{entry_window}} bars *and* above the {{trend_window}}-bar average.
  Both conditions use bars strictly before the one being judged.
- **Chandelier level** — the highest high since the breakout minus
  {{atr_multiplier}} ATR(22), ratcheting: the level never moves against the
  trade. Report the level and the distance from the last close, in percent.
- **Band state** — whether the close sits at the ±10% limit measured from the
  previous close (previous settlement for TAIFEX).

A name locked limit-down is the one case worth stating plainly: a protective
stop has no counterparty there and does not fill, so a position carries into
the next session whether or not its level was breached. Say so explicitly when
it happens; do not report such a name as "stopped out".

Distances are arithmetic on retrieved prices. Do not adjust them for costs,
and do not model a fill.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing symbol in a `Data gaps` section with the reason given.
- Never substitute a price from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook.
- Never present a stale bar as current. If the newest bar for a symbol predates
  the session everything else covers, print its date beside it and label it.
- Compute nothing for a symbol whose history came back shorter than the trend
  filter needs; report the shortfall instead of a level derived from too few
  bars.

## Output

Markdown, in this order:

1. `## Session` — the date this report covers, and the TAIEX close for it.
2. `## Breakouts` — symbols that newly satisfied both entry conditions on this
   session. Write `none` when there were none.
3. `## Open states` — table: symbol, close, Chandelier level, distance to the
   level in percent, bars since the breakout.
4. `## Band` — symbols that closed at the limit, up or down, and whether a stop
   for that symbol would have been unable to fill.
5. `## Data gaps` — always present; write `none` when nothing was missing.
6. `## Verdict` — the machine-readable tail, and the only section nothing may
   follow. One line per watch-list symbol:
   `- SYMBOL: STATE - one short reason`, with STATE one of `BREAKOUT`,
   `HOLDING`, `NEAR_STOP`, `LOCKED`, `FLAT`. Use `NEAR_STOP` when the close is
   within 2% of the Chandelier level and `LOCKED` when the symbol closed at the
   band, which outranks the others. A symbol with no data takes no line; it is
   already named under `Data gaps`.

## Boundaries

- State report on one completed session. No buy, sell, or hold calls, no price
  targets, no next-session predictions.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. There is no Taiwan broker connector in this project; a Taiwan
  position is entered by hand, by the reader.
- A breakout is an observation about price, not a signal to act on. Report the
  state and stop there.
- Never describe a Chandelier level as a guaranteed exit. It is a daily-bar
  construct, and the band rule above is exactly the case where it fails.
