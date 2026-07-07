---
title: Audit UI color contrast for WCAG accessibility in SQL
slug: wcag-contrast-audit
worker: color
data_version: "2026.1.0"
description: Score every foreground/background pair in your design system against WCAG AA/AAA, compute CIEDE2000 color differences, and flag failing combinations — as a plain SQL query over your color tokens.
keywords: [wcag, color contrast, accessibility, ciede2000, design tokens, duckdb]
difficulty: intermediate
est_minutes: 8
tier: recipe
dataset: {name: "Inline design-token pairs", provenance: "synthetic, in-tutorial VALUES"}
datePublished: 2026-07-06
dateModified: 2026-07-06
runtime: {wasm: auto}
---

## One of your buttons is failing WCAG right now

Here is a color pair straight out of a design system — a muted gray caption on
white. Run the numbers on it:

```sql {role=step expect=rows}
SELECT
  ROUND(color.main.contrast_ratio('#9aa4b2', '#ffffff'), 2) AS ratio,
  color.main.wcag_level('#9aa4b2', '#ffffff') AS level;
```
```result
ratio    level
2.09     fail
```

`2.09` against a `4.5` requirement — that caption is unreadable for a chunk of
your users, and nobody would catch it by eye. The good news: your tokens are just
rows, `vgi-color` implements the WCAG formulas exactly, so the audit is a query.

## Grade the whole palette at once

Feed it every foreground/background pair and get the ratio plus the WCAG level
for each. `wcag_level` returns `AAA` / `AA` / `AA Large` / `fail` for normal text.

```sql {role=step expect=rows}
WITH tokens(name, fg, bg) AS (
  VALUES
    ('primary button',  '#ffffff', '#3b6fed'),
    ('muted caption',   '#9aa4b2', '#ffffff'),
    ('success badge',   '#ffffff', '#2a9d3f'),
    ('warning text',    '#8a4b00', '#fff4e5')
)
SELECT name, fg, bg,
       ROUND(color.main.contrast_ratio(fg, bg), 2) AS ratio,
       color.main.wcag_level(fg, bg) AS level
FROM tokens
ORDER BY ratio DESC;
```
```result
name              fg         bg         ratio    level
warning text      #8a4b00    #fff4e5    7.14     AAA
primary button    #ffffff    #3b6fed    4.62     AA
success badge     #ffffff    #2a9d3f    3.03     AA Large
muted caption     #9aa4b2    #ffffff    2.09     fail
```

## Turn it into a CI gate

Wrap the same expression in a `WHERE` and you have a build-breaking check: any row
returned is a token pair a reviewer must fix before merge.

```sql {role=step expect=rows}
WITH tokens(name, fg, bg) AS (
  VALUES ('muted caption', '#9aa4b2', '#ffffff'), ('primary button', '#ffffff', '#3b6fed')
)
SELECT name, ROUND(color.main.contrast_ratio(fg, bg), 2) AS ratio
FROM tokens
WHERE color.main.contrast_ratio(fg, bg) < 4.5;
```
```result
name              ratio
muted caption     2.09
```

## Where this check stops

Contrast ratio is necessary, not sufficient — be precise about what this does and
doesn't certify:

- The `4.5` threshold is **normal text**. Large text (≥18.66px bold or ≥24px)
  only needs `3.0`, so `AA Large` rows above pass *for headings* and fail for body.
- WCAG 2.x contrast says nothing about **hue-based** confusions — a red/green pair
  can pass contrast and still be unreadable for deuteranopia. Use `delta_e` to
  measure perceptual distance where that matters.
- It scores **colors**, not rendered pixels: anti-aliasing, opacity, and
  background images can all lower effective contrast below what the tokens imply.

Within those limits it's exact — the luminance and ratio come straight from the
IEC/WCAG formulas, not an approximation. The full color-science surface
(`rgb_to_lab`, `nearest_color_name`, CIEDE2000) is in the
[vgi-color reference](https://github.com/Query-farm/vgi-color).
