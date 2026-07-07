---
title: Stop your backtest from trading on market holidays
slug: trading-calendar-gotchas
worker: cal
data_version: "2026.1.0"
description: Calendar days lie for finance — use exchange trading calendars in DuckDB to filter signals off market holidays, roll to real sessions, and get accurate close times for an honest backtest.
keywords: [backtest, trading calendar, market holidays, nyse, early close, duckdb]
difficulty: intermediate
est_minutes: 9
tier: recipe
dataset: {name: "Inline strategy signals", provenance: "synthetic, in-tutorial VALUES"}
datePublished: 2026-07-06
dateModified: 2026-07-06
runtime: {wasm: auto}
---

## Your backtest just filled an order on New Year's Day

A strategy emits a buy signal for `2026-01-01`, your fill logic adds it to the
blotter, and the P&L looks great — except the NYSE was closed, so that fill never
could have happened. Weekend and holiday closures quietly inflate backtests, and
"is the market open?" is not something `date` arithmetic can answer.

`vgi-calendar` carries real exchange calendars (NYSE, Nasdaq, LSE, Tokyo, …). The
default exchange is `XNYS`; `is_trading_day` is the guard you were missing:

```sql {role=step expect=scalar}
SELECT cal.main.is_trading_day(DATE '2026-01-01') AS market_open;
```
```result
market_open
false
```

## Flag the signals that couldn't have executed

Run your signal dates through it and the impossible fills surface immediately —
before they reach the blotter.

```sql {role=step expect=rows}
WITH signals(id, signal_date) AS (
  VALUES (1, DATE '2026-01-01'), (2, DATE '2026-01-02'), (3, DATE '2026-01-19')
)
SELECT id, signal_date, cal.main.is_trading_day(signal_date) AS tradable
FROM signals
ORDER BY id;
```
```result
id    signal_date    tradable
1     2026-01-01     false
2     2026-01-02     true
3     2026-01-19     false
```

Signal 3 is the subtle one: January 19, 2026 is Martin Luther King Jr. Day — a
weekday the market is closed, which a Mon–Fri filter would happily let through.

## Roll each signal to the next real session

Rather than drop them, execute on the next open session with `next_trading_day`.

```sql {role=step expect=rows}
WITH signals(id, signal_date) AS (
  VALUES (1, DATE '2026-01-01'), (3, DATE '2026-01-19')
)
SELECT id, signal_date, cal.main.next_trading_day(signal_date) AS executes_on
FROM signals
ORDER BY id;
```
```result
id    signal_date    executes_on
1     2026-01-01     2026-01-02
3     2026-01-19     2026-01-20
```

## The gotcha even pros forget: half-days

Some sessions close early — the Friday after Thanksgiving, Christmas Eve — and an
order assuming a 4:00 pm ET close is late. `trading_schedule` gives the real
open/close per session and flags the short ones; note there is *no* row for
Thanksgiving itself, because there was no session. `market_close` is a
`TIMESTAMPTZ`, so format it in the exchange's own zone for a stable, readable time.

```sql {role=step expect=rows}
SELECT
  session,
  strftime(market_close AT TIME ZONE 'America/New_York', '%H:%M') AS close_et,
  is_early_close
FROM cal.main.trading_schedule(DATE '2026-11-25', DATE '2026-11-29')
ORDER BY session;
```
```result
session       close_et    is_early_close
2026-11-25    16:00       false
2026-11-27    13:00       true
```

That `13:00` ET close on the 27th — three hours early. Any logic that hard-codes
the 16:00 closing bell just mispriced every position held into the close.

## Calendars are exchange-specific

Everything above defaulted to `XNYS`. The London and Tokyo calendars have
entirely different holidays and hours — pass an `exchange` argument
(`cal.main.is_trading_day(d, 'XLON')`) and run `SELECT * FROM cal.main.exchanges()`
to see what's covered. A backtest that spans venues needs the right calendar per
venue, not one global weekend rule.

Once your fills land on real sessions, the natural next step is expanding
recurring rebalance dates — which is where [RRULE recurrence](expand-recurring-events-rrule.html)
comes in. The full function catalog is in the
[vgi-calendar reference](https://github.com/Query-farm/vgi-calendar).
