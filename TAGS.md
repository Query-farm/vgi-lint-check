# VGI metadata tag reference

A complete, normative reference for the reserved `vgi.*` metadata tags a VGI
worker attaches to its catalog objects — what each one **means**, **where it may
occur**, **what value it takes**, and **what it is for**. Written for an LLM or
human maintaining a worker.

This is the source-of-truth companion to [`RULES.md`](RULES.md) (which lists the
lint rules). Where a tag is governed by a rule, the rule code is named so you can
look up its exact check. Rule codes are stable; this document tracks the code.

> Scope: this describes the metadata model `vgi-lint` grades. It does **not**
> change a worker's behavior — tags are documentation/discovery channels read
> from DuckDB system tables after the worker attaches.

---

## 1. How a worker carries metadata

A worker exposes two distinct metadata channels. **They are not interchangeable.**

1. **The object comment / description** — the native DuckDB `COMMENT` (or, for
   functions/settings/pragmas, the description the worker reports). One short line:
   *what this object is*. Every object kind has one.
2. **The `tags` MAP** — `MAP(VARCHAR, VARCHAR)` attached to an object. The
   framework reserves the `vgi.*` key namespace for the structured channels
   documented here (rich docs, examples, categories, provenance, …). Non-`vgi.`
   keys are free-form (see §6).

A reserved tag whose value is JSON (arrays/objects) is stored as a **JSON string**
inside the MAP value and decoded defensively: a malformed value becomes a lint
finding, never a crash.

### Object kinds

Tags in this document may apply to these kinds: **catalog**, **schema**,
**table**, **view**, **scalar_function**, **aggregate**, **macro**,
**table_function**. Columns, settings, pragmas, and attach options are documented
through their own *description/comment*, not through `vgi.*` tags.

### The two narratives (`comment` vs `vgi.doc_llm`/`vgi.doc_md`)

The object's `comment` is the one-liner. `vgi.doc_llm` and `vgi.doc_md` are the
*rich* narratives layered on top — they must **complement, not duplicate**, the
comment (VGI102): the comment says what it is; the docs say how to use it, the
columns/returns, caveats, and examples.

---

## 2. Quick map: which tags go where

`●` = applies / validated · `◦` = opt-in or recommended · blank = not applicable.

| Tag | catalog | schema | table/view | function/macro |
| --- | :---: | :---: | :---: | :---: |
| `vgi.doc_llm` | ● req | ● req | ● req* | ● req* |
| `vgi.doc_md` | ● req | ● req | ● req* | ● req* |
| `vgi.doc_links` | ◦ | ◦ | ◦ | ◦ |
| `vgi.result_columns_md` | | | | ● table_function |
| `vgi.title` | ● req | ● req | ◦ | |
| `vgi.keywords` | ◦ | ◦ | ◦ | |
| `vgi.category` | | | ● req | ● req |
| `vgi.categories` | | ● req (registry) | | |
| `vgi.classification_tags` | | ◦ | ◦ | ◦ |
| `vgi.example_queries` | | ◦ | ◦ | ◦ |
| `vgi.executable_examples` | ◦ | ◦ | ◦ | ◦ |
| `vgi.agent_test_tasks` | ● (only) | | | |
| `vgi.source_url` | ◦ rec | ◦ | ◦ | |
| `vgi.author` / `vgi.copyright` / `vgi.license` | ◦ rec | | | |
| `vgi.support_contact` / `vgi.support_policy_url` | ◦ rec | | | |

\* `req` on tables/views/functions is the **strict default** (VGI112/VGI113); relax via config.

---

## 3. Documentation tags

### `vgi.doc_llm`
- **Applies to:** catalog, schema, table, view, every function kind.
- **Value:** plain text (light Markdown ok). A concise narrative aimed at an LLM:
  *what the object is and when to reach for it* — purpose, key inputs/outputs,
  selection cues. Complements the `comment`; does not repeat it.
- **Required:** catalog (VGI002), every schema (VGI116); tables/views/functions
  under the strict default (VGI112).
- **Used for:** agent tool-selection — the primary signal an "ask AI" agent reads
  to decide whether an object is relevant.
- **Validated by:** VGI119 (substantive length), VGI103 (catalog/schema must be
  detailed), VGI102 (must add detail, not echo the comment), VGI114 (md should be
  richer than llm), VGI120 (no two objects share a description), VGI173 (a
  catalog/schema doc must not just *enumerate* the worker's objects), VGI174 (raw
  SQL must be fenced), VGI171 (any URLs must resolve).

### `vgi.doc_md`
- **Applies to:** catalog, schema, table, view, every function kind.
- **Value:** Markdown. A richer human-facing narrative: what it is, columns/returns,
  caveats, worked examples, links.
- **Required:** catalog (VGI003), every schema (VGI118); tables/views/functions
  under the strict default (VGI113).
- **Used for:** human docs / catalog listing pages.
- **Validated by:** VGI170 (well-formed Markdown — no empty/broken links, no
  unterminated fences), VGI171 (links/images resolve), VGI173, VGI174, VGI103,
  VGI114, VGI119.

### `vgi.doc_links`
- **Applies to:** any documented object (catalog … table_function).
- **Value:** **JSON array**; each entry is a URL string **or** a `{"title"?, "url"}`
  object. Example: `[{"title":"RFC-5545","url":"https://example.com/rfc"}]`.
- **Required:** optional.
- **Used for:** pointers to external/long-form documentation.
- **Validated by:** VGI172 (must be a JSON array of http(s) URLs / objects —
  *error*), VGI171 (each URL resolves).

### `vgi.result_columns_md`
- **Applies to:** table functions (relevant where the result schema is dynamic and
  DuckDB cannot expose columns up front).
- **Value:** Markdown documenting the returned columns (name, type, meaning).
- **Required:** a dynamic-schema table function with **no backing table** must set
  it (VGI307).
- **Used for:** giving agents/humans the result shape a table function returns when
  it can't be introspected.
- **Validated by:** VGI307, plus VGI170/VGI171 (Markdown validity / link resolution).

---

## 4. Discovery & navigation tags

### `vgi.title`
- **Applies to:** catalog, schema, table, view.
- **Value:** a human/marketing display name string (distinct from the machine name).
- **Required:** catalog and every schema (VGI124); optional on tables/views.
- **Used for:** listings/UIs that want a friendly name.
- **Validated by:** VGI125 (when set, must differ from the machine name).

### `vgi.keywords`
- **Applies to:** catalog, schema, table, view.
- **Value:** **JSON array of strings** — search terms / synonyms.
  Example: `["seismic","tremor","magnitude"]`. A comma-separated string is **not**
  accepted.
- **Required:** optional, but expected under the strict default (VGI126).
- **Used for:** search / synonym matching during discovery.
- **Validated by:** VGI138 (must be a JSON array — *error*), VGI127 (non-empty, no
  duplicates).

### `vgi.category`  — an object's *primary* category
- **Applies to:** table, view, every function kind. **Not** the catalog or a schema.
- **Value:** a **single string** equal to a `name` defined in the owning schema's
  `vgi.categories` registry. One primary category per object. (For multiple
  cross-cutting labels use `vgi.classification_tags`, not this.)
- **Required:** **yes** — every categorizable object in a schema must carry one
  (VGI411), so categories drive complete navigation/SEO.
- **Used for:** the navigation layer — the section an object sits under
  (catalog → schema → **category** → object).
- **Validated by:** VGI409 (value must be defined in the schema registry — *error*,
  with a did-you-mean hint), VGI411 (coverage), VGI408 (placement: not on
  schema/catalog).

### `vgi.categories`  — a schema's category registry
- **Applies to:** **schema only** (never the catalog).
- **Value:** an **ordered JSON array** of category objects; **array order is the
  display order**. Each entry:
  - `name` — **required**, a stable lowercase slug; the join key `vgi.category` references.
  - `title` — optional human label (defaults to a title-cased `name`).
  - `description` — strongly recommended (one line; WARN if blank).
  - `keywords` — optional JSON array of strings (symmetric with `vgi.keywords`).
  - `doc_md` — optional longer Markdown landing copy for the section.
  ```json
  [
    {"name":"geocoding","title":"Geocoding & Addresses","description":"Forward/reverse geocoding."},
    {"name":"routing","title":"Routing & Distance","description":"Shortest-path and distance."}
  ]
  ```
- **Required:** **yes** — every schema with objects must declare a registry
  (VGI413); for SEO/navigation each object then references a category by `name`.
- **Used for:** declaring the schema's ordered, described navigation sections.
- **Validated by:** VGI413 (a schema with objects must declare a registry),
  VGI408 (well-formed array, unique non-empty names, not on the catalog —
  *error*), VGI410 (each category should have a description), VGI412 (a declared
  category with **no member objects is an error** — a dead/empty section).

### `vgi.classification_tags`  — cross-cutting facets
- **Applies to:** any object **except the catalog**.
- **Value:** **JSON array of strings** — multiple cross-cutting facet labels for
  search/filtering. Example: `["geospatial","timeseries","experimental"]`.
- **Required:** opt-in.
- **Used for:** faceted search, orthogonal to the single primary `vgi.category`.
- **Validated by:** VGI406 (must be a JSON array of strings, not on the catalog —
  *error*).
- **Renamed:** was `vgi.category_tags` (the old key still resolves; see §7).

---

## 5. Example & test tags

### `vgi.example_queries`  — illustrative examples
- **Applies to:** tables, views, functions/macros, and (opt-in) schemas.
- **Value:** **JSON array** of `{"description","sql"}` objects. These are *shown*,
  not executed by the example-execution rules.
- **Required:** optional; recommended (VGI501 for tables/views, VGI303 for macros,
  VGI306 for scalar/aggregate functions; VGI506 for schemas is opt-in).
- **Used for:** human/agent-facing usage demonstrations.
- **Validated by:** VGI502 (valid JSON list — *error*), VGI503 (each entry needs a
  non-empty `description` and `sql` — *error*), VGI504 (an example should call the
  object it documents), VGI505 (qualify references as `catalog.schema.object` so
  they run when attached), VGI150 (don't ship only trivial `SELECT *`).

### `vgi.executable_examples`  — guaranteed-runnable examples
- **Applies to:** catalog, schema, table, view, macro, scalar/aggregate/table function.
- **Value:** **JSON array** of `{"name"?, "description", "sql"}` entries, where `sql`
  is a **string**, a **list of strings**, or a **list of step objects**
  `{"description"?, "sql", "expected_result"?}` run in order. `expected_result` (a
  JSON value) asserts that step's output.
  ```json
  [{"description":"Easter 2026","sql":"SELECT cal.main.easter(2026)","expected_result":[["2026-04-05"]]}]
  ```
- **Required:** optional; a worker should ship at least one at the catalog level
  (VGI509).
- **Used for:** examples that are actually executed against the worker under
  `--execute` — a live correctness signal.
- **Validated by:** VGI507 (valid shape — *error*), VGI906 (every statement must run
  — *error*), VGI907 (output matches `expected_result`), VGI510 (assert-bearing
  examples should `ORDER BY` for stable rows), VGI508 (too many on one object),
  VGI908 (slow example).

### `vgi.agent_test_tasks`  — the agent-suitability suite
- **Applies to:** the **catalog only**.
- **Value:** **JSON array** of task objects:
  - `name` — **required**, unique task id.
  - `prompt` — **required**, the natural-language task (the *only* field shown to
    the analyst).
  - `reference_sql` — optional canonical solution: a string, list of strings, or
    list of `{"description"?, "sql", "expected_result"?}` steps. **Grader-only.**
  - `success_criteria` — optional judge rubric. **Grader-only.**
  - `check_sql` — optional post-session assertion. **Grader-only.**
  - `unordered` / `ignore_column_names` — optional booleans relaxing strict
    result comparison for that task.
- **Required:** optional (needed only to run `vgi-lint simulate`).
- **Used for:** `vgi-lint simulate` — runs an LLM analyst through these tasks to
  grade how *discoverable/usable* the worker is for agents.
- **Critical invariant:** `reference_sql`, `success_criteria`, and `check_sql` are
  **grader-only** and must never appear in any listing or tool output the analyst
  sees. Do not stash hints in them expecting the agent to read them.
- **Validated by:** VGI407 (valid `{name, prompt}` array — *error*).

---

## 6. Provenance, legal & support tags (catalog-level)

| Tag | Value | Used for | Rules |
| --- | --- | --- | --- |
| `vgi.source_url` | http(s) URL | where the catalog/object is implemented (repo/file) | VGI004 (catalog should advertise), VGI129 (valid URL), VGI139 (keep it on the catalog, don't repeat on every object), VGI171 (resolves). Opt-in on schema/table/view (VGI128, off by default). |
| `vgi.author` | string | author / maintainer attribution | VGI160 (declare), part of the catalog provenance set |
| `vgi.copyright` | string | copyright notice | VGI160 |
| `vgi.license` | string | license name / SPDX id (prefer SPDX, or `LicenseRef-…` for custom) | VGI160, VGI013 (SPDX form) |
| `vgi.support_contact` | email **or** http(s) URL | where to report issues/bugs | VGI009 (advertise), VGI010 (URL form valid) |
| `vgi.support_policy_url` | http(s) URL | link to the support / SLA policy | VGI009, VGI010 |

---

## 7. Free-form (non-`vgi.`) tags

These are **not** in the `vgi.*` namespace and are configured, not fixed:

- **Classifying tags** — bare keys `domain`, `provider`, `topic` (the default
  `classifying_tag_keys`; `category` was intentionally removed in favor of the
  structured `vgi.category`). Applied to schemas/tables/views. A small **reused**
  vocabulary, not unique per object. Governed by VGI123 (presence) and VGI132
  (reused vocabulary). Example: `"domain": "date-and-time"`.
- **Required tags** — `required_schema_tags` / `required_table_tags` config let a
  worker mandate specific arbitrary keys (VGI401). Empty by default (opt-in).
- **Allow-list** — `allowed_tag_keys` config, when set, flags any tag key outside
  it (VGI403).

---

## 8. Deprecated keys & migration

The old key keeps working (it transparently resolves to the canonical key) but
**VGI405** flags it for migration. Each will stop being recognized in **v1.0**.

| Deprecated key | Canonical key |
| --- | --- |
| `vgi.description_llm` | `vgi.doc_llm` |
| `vgi.description_md` | `vgi.doc_md` |
| `vgi.columns_md` | `vgi.result_columns_md` |
| `vgi.category_tags` | `vgi.classification_tags` |

---

## 9. Namespace rules & invariants

- **The `vgi.*` namespace is framework-owned.** A `vgi.*` key that is not one of
  the reserved keys above is treated as a typo (VGI404, with a did-you-mean hint).
  Do not invent new `vgi.*` keys — use a free-form (non-prefixed) key instead.
- **No empty reserved tags.** A reserved `vgi.*` tag present with a blank value is a
  finding (VGI402); omit it instead of setting it empty.
- **JSON tags are strings.** Array/object-valued tags (`vgi.keywords`,
  `vgi.doc_links`, `vgi.example_queries`, `vgi.executable_examples`,
  `vgi.agent_test_tasks`, `vgi.categories`, `vgi.classification_tags`) are stored as
  JSON-encoded strings in the MAP value.
- **Strict by default.** Documentation tags are required broadly under the strict
  profile; a worker opts out per object/rule via `[tool.vgi-lint-check]`
  `ignore`/`severity`, not by leaving tags blank.

---

## 10. Worked example (a schema with categories)

```python
# Schema-level tags
{
  "vgi.title": "Calendar — main",
  "vgi.doc_llm": "Holiday, business-day, recurrence, and trading-calendar helpers …",
  "vgi.doc_md": "## Calendar functions\n\n…",
  "vgi.keywords": "[\"holiday\",\"business day\",\"trading day\",\"iso week\"]",
  "vgi.categories": "[{\"name\":\"holidays\",\"title\":\"Holidays\",\"description\":\"Public-holiday tests and names.\"},{\"name\":\"trading\",\"title\":\"Trading calendars\",\"description\":\"Exchange sessions and market hours.\"}]",
  "domain": "date-and-time"
}

# A function in that schema
{
  "vgi.doc_llm": "True when a date is a public holiday in a country …",
  "vgi.doc_md": "## is_holiday\n\n…",
  "vgi.category": "holidays",
  "vgi.classification_tags": "[\"calendar\",\"lookup\"]"
}
```
