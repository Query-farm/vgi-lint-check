"""Load and validate the VGI semantic-layer JSON Schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_NAMES = (
    "definitions",
    "expression",
    "catalog",
    "entity",
    "member",
    "member-template",
    "members",
    "relationship",
    "relationships",
    "query",
    "plan",
    "diagnostic",
    "result",
    "environment",
)


class SemanticSchemaError(ValueError):
    """Raised when a semantic schema or instance is invalid."""


@lru_cache(maxsize=1)
def schemas() -> dict[str, dict[str, Any]]:
    """Return every packaged semantic schema after checking its own shape."""
    root = files("vgi_lint_check").joinpath("schema", "semantic")
    loaded: dict[str, dict[str, Any]] = {}
    for name in SCHEMA_NAMES:
        decoded = json.loads(root.joinpath(f"{name}.json").read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise SemanticSchemaError(f"semantic schema {name!r} is not an object")
        Draft202012Validator.check_schema(decoded)
        loaded[name] = cast(dict[str, Any], decoded)
    return loaded


@lru_cache(maxsize=1)
def _registry() -> Registry:
    registry = Registry()
    for schema in schemas().values():
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(str(schema["$id"]), resource)
    return registry


def schema(name: str) -> dict[str, Any]:
    """Return one schema by its short name."""
    try:
        return schemas()[name]
    except KeyError as exc:
        raise SemanticSchemaError(
            f"unknown semantic schema {name!r}; expected one of {', '.join(SCHEMA_NAMES)}"
        ) from exc


def validate_instance(name: str, instance: Any) -> list[str]:
    """Return deterministic, human-readable validation errors for an instance."""
    validator = Draft202012Validator(schema(name), registry=_registry())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.absolute_path))
    rendered: list[str] = []
    for error in errors:
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
        )
        rendered.append(f"{path}: {error.message}")
    return rendered


def export_bundle() -> dict[str, Any]:
    """Return a stable schema-name-to-document bundle for other runtimes."""
    return {name: schemas()[name] for name in SCHEMA_NAMES}
