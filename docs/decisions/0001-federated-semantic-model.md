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

The first query compiler supports one measure-owning root grain plus to-one dimension enrichment.
It rejects fanout and multi-root measure queries, while preserving a branch-shaped IR for later
multi-fact stitching. Compiled SQL is deterministic and parameterized. Compile-only validation does
not query DuckDB.

## Consequences

The metadata stays close to the physical interface and naturally travels with a worker. DuckDB 1.5
requires packed relation/function members; later DuckDB releases can expose native column tags, so
consumers must normalize both. Federation is explicit and safe across runtime aliases, but users may
need bindings when several instances share one logical identity. The conservative compiler rejects
some valid SQL rather than guessing cardinality or repairing fanout invisibly.

Multi-fact metrics, temporal/spatial predicates, correlated table functions and arbitrary SQL
expressions remain future work. They can be added without changing existing stable IDs or the
single-branch request shape.
