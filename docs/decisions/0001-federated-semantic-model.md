# ADR 0001: Federated semantic metadata in DuckDB tags

Status: accepted

## Context

VGI workers already publish descriptions and agent guidance through DuckDB object tags. A semantic
model stored elsewhere can drift from the database objects it describes, while attached catalogs
may use arbitrary runtime aliases and multiple instances of the same worker may coexist.

## Decision

Publish semantic identity, entities, members and relationship assertions as reserved JSON-valued
tags. Validate their local shapes with Draft 2020-12 JSON Schemas and their graph semantics with
`vgi-lint-check`. Address catalogs and entities by stable IDs; bind them to attachment aliases only
at query time.

Relationships are bilateral graph assertions, not stored joins. One declaration is traversable in
both directions. Compatible reciprocal declarations corroborate one relationship; conflicts remain
visible. A designated federation catalog may assert a third-party relationship. Tags cannot
override other assertions or grant themselves authority. Resolution status and attestation are
reported separately.

The query compiler supports one to ten measure-owning root grains plus to-one dimension enrichment.
It aggregates each fact root independently and stitches those aggregates only at dimensions with
the same stable semantic identity, using a distinct key spine and null-safe joins. Missing fact
values remain null unless an additive numeric measure explicitly opts into a typed zero. Shared
population filters run in every branch; selected-measure filters run after stitching. The first
multi-fact release excluded branch-specific populations and new cross-fact arithmetic.

The contract now permits explicit conformance between different member identities. Equivalent
members declare the same `conformance_id`, and a query names every per-root substitution; names are
never treated as proof. Model-owned filters are allowed on base aggregates and compile to
parameterized SQL `FILTER`. Query-level cross-fact derived measures are typed, post-stitch
expressions over selected base outputs. They require explicit missing-value policy for every input,
cannot chain, and provide no raw-SQL escape hatch.

The compiler also supports bounded correlated table-function pipelines driven by typed query-local
inputs or another semantic entity. Invocation edges are dataflow, not semantic relationships. The
compiler derives column-input capability and named/positional calling convention from live function
metadata, preserves every upstream driving grain by default, and rejects cycles, unbounded drivers,
excessive aggregate invocations, incompatible branch grains, fanout, and unsafe zero filling.
Compiled SQL is deterministic and parameterized. Compile-only validation does not query DuckDB.

## Consequences

The metadata stays close to the physical interface and naturally travels with a worker. DuckDB 1.5
requires packed relation/function members; later DuckDB releases can expose native column tags, so
consumers must normalize both. Federation is explicit and safe across runtime aliases, but users may
need bindings when several instances share one logical identity. The conservative compiler rejects
some valid SQL rather than guessing cardinality or repairing fanout invisibly.

Branch-specific population filters, temporal predicates, derived-measure chaining, and arbitrary
SQL expressions remain future work. Typed spatial and repeated-field relationship predicates have
also been added without introducing a raw SQL escape hatch. These additions do not change existing
stable IDs or the single-fact request shape.
