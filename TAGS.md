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
| `vgi.result_columns_schema` | | | | ● table_function |
| `vgi.result_dynamic_columns_md` | | | | ● table_function |
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
| `vgi.semantic_catalog` | ◦ | | | |
| `vgi.semantic_entity` / `vgi.semantic_members` | | | ◦ | ◦ table_function |
| `vgi.semantic_member` | | | ◦ column | |
| `vgi.semantic_relationships` | ◦ | | ◦ | ◦ table_function |

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

### `vgi.result_columns_schema`
- **Applies to:** table functions whose result schema is **static** (the same
  columns regardless of arguments).
- **Value:** a JSON array of `{name, type, description}` objects — one per returned
  column. `type` must be a real DuckDB type; `description` must be non-blank.
- **Required:** a table function with **no backing table** must declare its result
  schema — this tag (static) *or* `vgi.result_dynamic_columns_md` (dynamic), exactly
  one (VGI307).
- **Used for:** giving agents/humans (and the linter) the exact result shape when
  DuckDB can't expose it up front.
- **Validated by:** VGI307 (declared / not both), VGI321 (JSON shape), VGI322 (types
  are real), VGI323 (every column described), VGI324 (matches a backing table when
  present), VGI910 (matches what the function actually returns, under `--execute`).

### `vgi.result_dynamic_columns_md`
- **Applies to:** table functions whose result schema **varies by argument**.
- **Value:** Markdown that describes *how* the schema varies **and** contains one or
  more `Name | Type | Description` tables — one per variant (the Markdown rendering
  of a `vgi.result_columns_schema`). A section heading above a table is an optional
  label for when that variant applies.
- **Required:** the dynamic alternative to `vgi.result_columns_schema` (VGI307).
- **Used for:** documenting each argument-dependent result shape in a structured,
  lintable form.
- **Validated by:** VGI307, VGI326 (≥1 well-formed variant table), VGI322/VGI323
  (each variant column's type is real and described), VGI170/VGI171 (Markdown /
  links), VGI910 (returned columns are covered by some variant, under `--execute`).

> **Retired:** `vgi.result_columns_md` (and its old alias `vgi.columns_md`) are no
> longer read — they were free-form Markdown with no type/description validation.
> VGI414 errors on either; migrate to the two structured tags above.

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
- **Required:** yes under the default strict profile (VGI152).
- **Used for:** `vgi-lint simulate` — runs an LLM analyst through these tasks to
  grade how *discoverable/usable* the worker is for agents.
- **Private grading sidecar:** put `reference_sql`, `success_criteria`, `check_sql`,
  `unordered`, and `ignore_column_names` in `vgi-agent-tests.yaml`, keyed by the
  public task `name`. The linter discovers that conventional filename beside its
  config; select another file with `[simulate] agent_tasks_file` or
  `vgi-lint simulate --agent-tasks-file PATH`. Without private fields, simulation
  falls back to the judge rubric based on the public prompt and observed result.
- **Critical invariant:** the database tag contains only `name` and `prompt`.
- **Validated by:** VGI407 (valid shape) and VGI416 (no embedded grader fields).

---

## 6. Provenance, legal & support tags (catalog-level)

| Tag | Value | Used for | Rules |
| --- | --- | --- | --- |
| `vgi.source_url` | http(s) URL | where the catalog/object is implemented (repo/file) | VGI004 (catalog should advertise), VGI129 (valid URL), VGI139 (do not repeat the catalog URL unchanged), VGI171 (resolves). Distinct per-object links are accepted; completeness remains opt-in via VGI128. |
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

### Extension-injected tags (`vgi_*`)

The VGI DuckDB extension injects a few reserved **`vgi_`-prefixed** keys (note the
underscore — distinct from the worker-authored `vgi.` namespace, so VGI404 ignores
them). Workers do not set these; the extension renders them onto the DuckDB
catalog at attach time.

- **`vgi_required_filters`** — on tables/views. The extension serializes
  `Table.required_filters` (an AND of OR-groups of column paths) as a JSON array of
  arrays, e.g. `[["accession_number"],["ticker","cik"]]`, onto
  `duckdb_tables().tags`. Lets a caller discover a table's required WHERE-filter
  groups (via `SELECT tags['vgi_required_filters'] FROM duckdb_tables()`) *before*
  hitting the bind-time `BinderException`. Agent-facing `describe_table` tools (the
  web frontend and `vgi-lint simulate`) surface it decoded as `required_filters`
  next to a one-line `required_filters_rule`, so an agent never needs to know the
  tag key or infer what the nested arrays mean. **Validated by:** VGI415
  (well-formed JSON list-of-non-empty-lists-of-strings; also nudges near-miss key
  spellings).
- **`vgi_resolved_data_version` / `vgi_resolved_implementation_version`** — on the
  catalog (`duckdb_databases().tags`); the versions the worker resolved at attach.

---

## 8. Deprecated keys & migration

The old key keeps working (it transparently resolves to the canonical key) but
**VGI405** flags it for migration. Each will stop being recognized in **v1.0**.

| Deprecated key | Canonical key |
| --- | --- |
| `vgi.description_llm` | `vgi.doc_llm` |
| `vgi.description_md` | `vgi.doc_md` |
| `vgi.category_tags` | `vgi.classification_tags` |

**Retired keys** (no transparent fallback — the shape changed; **VGI414** errors):

| Retired key | Replacement |
| --- | --- |
| `vgi.result_columns_md` | `vgi.result_columns_schema` (static) / `vgi.result_dynamic_columns_md` (dynamic) |
| `vgi.columns_md` | `vgi.result_columns_schema` (static) / `vgi.result_dynamic_columns_md` (dynamic) |

---

## 9. Namespace rules & invariants

### Semantic model tags

VGI also reserves `vgi.semantic_catalog`, `vgi.semantic_entity`,
`vgi.semantic_members`, `vgi.semantic_member`, and `vgi.semantic_relationships` for a federated
measure/dimension model. Their JSON Schemas, identity rules, relationship reconciliation and
compiler contract are specified in [`docs/semantic-model.md`](docs/semantic-model.md). Human worker
authors should start with
[`docs/semantic-model-authoring.md`](docs/semantic-model-authoring.md); coding agents should use
[`docs/semantic-model-agent-authoring.md`](docs/semantic-model-agent-authoring.md).

- **The `vgi.*` namespace is framework-owned.** A `vgi.*` key that is not one of
  the reserved keys above is treated as a typo (VGI404, with a did-you-mean hint).
  Do not invent new `vgi.*` keys — use a free-form (non-prefixed) key instead.
- **No empty reserved tags.** A reserved `vgi.*` tag present with a blank value is a
  finding (VGI402); omit it instead of setting it empty.
- **JSON tags are strings.** Array/object-valued tags (`vgi.keywords`,
  `vgi.doc_links`, `vgi.example_queries`, `vgi.executable_examples`,
  `vgi.agent_test_tasks`, `vgi.categories`, `vgi.classification_tags`,
  `vgi.result_columns_schema`) are stored as
  JSON-encoded strings in the MAP value.
- **Build-time contract, not a worker protocol version.** `vgi-lint spec --format
  json` publishes the latest key/alias/retirement registry for consumers to vendor
  and check in CI. Workers do not need to advertise a metadata-spec version.
- **Agent context is bounded.** Catalog `vgi.doc_llm` is capped at 8,000 characters;
  detail values and individual examples are capped at 4,000 characters and detail
  tools return at most five examples. VGI417 warns before consumers truncate them.
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
    "vgi.keywords": '["holiday","business day","trading day","iso week"]',
    "vgi.categories": '[{"name":"holidays","title":"Holidays","description":"Public-holiday tests and names."},{"name":"trading","title":"Trading calendars","description":"Exchange sessions and market hours."}]',
    "domain": "date-and-time",
}

# A function in that schema
{
    "vgi.doc_llm": "True when a date is a public holiday in a country …",
    "vgi.doc_md": "## is_holiday\n\n…",
    "vgi.category": "holidays",
    "vgi.classification_tags": '["calendar","lookup"]',
}
```
