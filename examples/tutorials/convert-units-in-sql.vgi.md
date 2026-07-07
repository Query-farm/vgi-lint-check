---
title: Convert physical units and catch bad data in SQL
slug: convert-units-in-sql
worker: units
data_version: "2026.1.0"
description: Convert values between any two unit strings at runtime, normalize a messy measurements column to a common base unit, and turn incompatible conversions into loud errors instead of silent wrong numbers — inside DuckDB.
keywords: [unit conversion, dimensional analysis, data cleaning, duckdb, measurements]
difficulty: beginner
est_minutes: 6
tier: quickstart
dataset: {name: "Inline measurement rows", provenance: "synthetic, in-tutorial VALUES"}
datePublished: 2026-07-06
dateModified: 2026-07-06
runtime: {wasm: auto}
---

## The dangerous conversion is the one that looks fine

A units bug rarely throws. A hand-rolled factor table quietly turns kilometers
into the wrong number of miles, the report ships, and nobody notices for a
quarter. `vgi-units` does runtime, string-driven conversion across 14 physical
dimensions — and, crucially, it refuses to guess when a conversion is nonsense.

Start with the easy, correct case: miles to kilometers, the exact international
definition.

```sql {role=step expect=scalar}
SELECT units.main.convert(1, 'mi', 'km') AS km;
```
```result
km
1.609344
```

## Temperature: the offset that trips everyone up

°C / °F / K have an additive offset, not just a scale factor. Miss it and 0 °C
becomes 0 °F. The engine does the affine round-trip, so it doesn't.

```sql {role=step expect=scalar}
SELECT ROUND(units.main.convert(0, 'C', 'F'), 1) AS fahrenheit;
```
```result
fahrenheit
32.0
```

## Fold a messy column onto one base unit

Real data arrives in mixed units. `to_base` normalizes each value to the SI base
of its dimension, so a `km`/`m`/`mi` jumble becomes comparable meters in one pass.

```sql {role=step expect=rows}
WITH readings(id, value, unit) AS (
  VALUES (1, 5, 'km'), (2, 500, 'm'), (3, 1, 'mi')
)
SELECT id, value, unit, ROUND(units.main.to_base(value, unit), 2) AS meters
FROM readings
ORDER BY meters;
```
```result
id    value    unit    meters
2     500      m       500.0
3     1        mi      1609.34
1     5        km      5000.0
```

## Two failure modes, on purpose

This is the whole design philosophy, and it's worth seeing both halves side by
side. An **unknown** unit is missing data — it yields `NULL` so a dirty row
doesn't abort the scan:

```sql {role=step expect=rows}
SELECT units.main.convert(5, 'km', 'furlongs-per-fortnight') AS bad_unit;
```
```result
bad_unit
NULL
```

But an **incompatible** conversion is a logic error — both units are real, the
request is nonsense — so it does *not* return a number. It raises:

```sql {role=illustrative expect=error}
-- length -> mass is meaningless; a wrong number here would be worse than a crash
SELECT units.main.convert(5, 'km', 'kg');
-- ERROR: cannot convert between dimensions 'length' and 'mass'
```

That line is the entire point of the worker. A conversion that can't be right
fails loudly instead of poisoning a downstream report — and everything that
*can* be right (`parse_quantity`, `compatible`, 300 units) is in the
[vgi-units reference](https://github.com/Query-farm/vgi-units).
