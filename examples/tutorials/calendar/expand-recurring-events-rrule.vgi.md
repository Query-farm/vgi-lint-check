---
title: Expand recurring events with RRULE in DuckDB
slug: expand-recurring-events-rrule
worker: cal
data_version: "2026.1.0"
description: Turn one RFC-5545 RRULE into a table of occurrences, then compose it with business-day logic to shift billing or payroll dates off weekends and holidays — all in SQL.
keywords: [rrule, rfc 5545, recurring events, billing dates, payroll, duckdb]
difficulty: intermediate
est_minutes: 8
tier: recipe
dataset: {name: "Inline recurrence rules", provenance: "synthetic, in-tutorial VALUES"}
datePublished: 2026-07-06
dateModified: 2026-07-06
runtime: {wasm: auto}
---

## One rule, every occurrence

Calendars and schedulers store recurring events as a single RFC-5545 rule —
`FREQ=WEEKLY;COUNT=4` — not as a list of dates. To *use* those events in
analytics you need the expansion: the actual timestamps. Writing that expansion
by hand (nth-weekday-of-month, interval skips, counts) is exactly the fiddly
date logic that breeds bugs.

`rrule` is a table function that takes the rule and a start, and returns one row
per occurrence with a sequence number.

```sql {role=step expect=rows}
SELECT seq, occurrence
FROM cal.main.rrule(TIMESTAMP '2026-01-05', 'FREQ=WEEKLY;COUNT=4')
ORDER BY seq;
```
```result
seq    occurrence
0      2026-01-05 00:00:00
1      2026-01-12 00:00:00
2      2026-01-19 00:00:00
3      2026-01-26 00:00:00
```

## The rule grammar does the hard part

RFC-5545 handles patterns that are painful in raw SQL — "the second Tuesday of
every month" is one `BYDAY` clause, not a window function.

```sql {role=step expect=rows}
SELECT seq, occurrence
FROM cal.main.rrule(TIMESTAMP '2026-01-01', 'FREQ=MONTHLY;BYDAY=2TU;COUNT=3')
ORDER BY seq;
```
```result
seq    occurrence
0      2026-01-13 00:00:00
1      2026-02-10 00:00:00
2      2026-03-10 00:00:00
```

## Where RRULE stops and your policy begins

Here is the real-world catch: recurrence gives you the *nominal* dates, but a
business almost never bills or pays on a weekend or holiday. RRULE won't shift
those for you — and it shouldn't, because "move to the next business day" is your
policy, not the calendar's. This is where the recurrence and business-day sides
of `vgi-calendar` compose.

Generate monthly billing dates on the 1st, then adjust each to a real working day:

```sql {role=step expect=rows}
SELECT
  occurrence::DATE AS nominal,
  cal.main.is_business_day(occurrence::DATE) AS is_workday,
  CASE WHEN cal.main.is_business_day(occurrence::DATE)
       THEN occurrence::DATE
       ELSE cal.main.add_business_days(occurrence::DATE, 1)
  END AS billing_day
FROM cal.main.rrule(TIMESTAMP '2026-01-01', 'FREQ=MONTHLY;BYMONTHDAY=1;COUNT=3')
ORDER BY seq;
```
```result
nominal       is_workday    billing_day
2026-01-01    false         2026-01-02
2026-02-01    false         2026-02-02
2026-03-01    false         2026-03-02
```

All three 1st-of-month dates were unworkable — New Year's Day, then two Sundays —
and each rolled to the correct next business day in a single expression.

## The pattern to remember

`rrule` answers *when does this repeat?*; `is_business_day` / `add_business_days`
answer *when can we actually act on it?* Keep them separate and compose them —
that split is the whole trick, and it generalizes to payroll runs, SLA clocks,
and rebalance schedules. If your events also depend on market sessions, pair this
with the [trading-calendar recipe](trading-calendar-gotchas.html), or browse the
full [vgi-calendar reference](https://github.com/Query-farm/vgi-calendar).
