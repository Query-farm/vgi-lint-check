"""Load and expose the machine-readable VGI metadata tag contract."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, NoReturn, cast

SUPPORTED_CONTRACT_REVISION = 1
_SECTIONS = ("tags", "aliases", "retired", "extension_tags")
_FORMATS = {"json", "markdown", "text", "url"}
_SCOPES = {"catalog", "schema", "table", "view", "function"}


class TagContractError(ValueError):
    """Raised when the bundled VGI tag contract is internally inconsistent."""


def _invalid(message: str) -> NoReturn:
    raise TagContractError(message)


def _string(item: dict[str, Any], field: str, location: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{location}.{field} must be a non-empty string")
    return value


def _string_list(item: dict[str, Any], field: str, location: str) -> list[str]:
    value = item.get(field)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(entry, str) and entry.strip() for entry in value)
    ):
        _invalid(f"{location}.{field} must be a non-empty list of strings")
    if len(value) != len(set(value)):
        _invalid(f"{location}.{field} contains duplicate values")
    return cast(list[str], value)


def validate_contract(spec: dict[str, Any] | None = None) -> None:
    """Validate the bundled tag contract's schema and cross-references.

    Args:
        spec: Contract to validate. When omitted, read the bundled contract.

    Raises:
        TagContractError: If the contract is malformed or inconsistent.
    """
    candidate = _raw_contract() if spec is None else spec
    revision = candidate.get("contract_revision")
    if revision != SUPPORTED_CONTRACT_REVISION:
        _invalid(
            f"unsupported contract_revision {revision!r}; "
            f"expected {SUPPORTED_CONTRACT_REVISION}"
        )
    if not isinstance(candidate.get("title"), str) or not candidate["title"].strip():
        _invalid("title must be a non-empty string")

    entries: dict[str, list[dict[str, Any]]] = {}
    seen_symbols: dict[str, str] = {}
    seen_keys: dict[str, str] = {}
    for section in _SECTIONS:
        raw_entries = candidate.get(section)
        if not isinstance(raw_entries, list):
            _invalid(f"{section} must be a list")
        typed_entries: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_entries):
            location = f"{section}[{index}]"
            if not isinstance(raw_item, dict):
                _invalid(f"{location} must be an object")
            item = cast(dict[str, Any], raw_item)
            symbol = _string(item, "symbol", location)
            key = _string(item, "key", location)
            if not symbol.startswith("TAG_") or not symbol.removeprefix("TAG_").replace(
                "_", ""
            ).isalnum():
                _invalid(f"{location}.symbol is not a valid TAG_* symbol")
            if symbol in seen_symbols:
                _invalid(f"duplicate symbol {symbol!r} in {seen_symbols[symbol]} and {location}")
            if key in seen_keys:
                _invalid(f"duplicate key {key!r} in {seen_keys[key]} and {location}")
            seen_symbols[symbol] = location
            seen_keys[key] = location
            typed_entries.append(item)
        entries[section] = typed_entries

    active_keys = {item["key"] for item in entries["tags"]}
    for index, item in enumerate(entries["tags"]):
        location = f"tags[{index}]"
        tag_format = _string(item, "format", location)
        if tag_format not in _FORMATS:
            _invalid(f"{location}.format must be one of {sorted(_FORMATS)!r}")
        scopes = _string_list(item, "scopes", location)
        unknown_scopes = sorted(set(scopes) - _SCOPES)
        if unknown_scopes:
            _invalid(f"{location}.scopes contains unknown scopes {unknown_scopes!r}")
        if "public_fields" in item:
            _string_list(item, "public_fields", location)

    for index, item in enumerate(entries["aliases"]):
        location = f"aliases[{index}]"
        canonical = _string(item, "canonical", location)
        if canonical not in active_keys:
            _invalid(f"{location}.canonical references unknown active key {canonical!r}")

    for index, item in enumerate(entries["retired"]):
        location = f"retired[{index}]"
        replacements = _string_list(item, "replacement", location)
        unknown_replacements = sorted(set(replacements) - active_keys)
        if unknown_replacements:
            _invalid(
                f"{location}.replacement references unknown active keys {unknown_replacements!r}"
            )

    for index, item in enumerate(entries["extension_tags"]):
        location = f"extension_tags[{index}]"
        tag_format = _string(item, "format", location)
        if tag_format not in _FORMATS:
            _invalid(f"{location}.format must be one of {sorted(_FORMATS)!r}")


@lru_cache(maxsize=1)
def _raw_contract() -> dict[str, Any]:
    """Read the bundled tag contract once per process."""
    resource = files("vgi_lint_check").joinpath("tag_contract.json")
    decoded = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        _invalid("contract root must be an object")
    return cast(dict[str, Any], decoded)


def contract() -> dict[str, Any]:
    """Return the validated bundled tag contract."""
    spec = _raw_contract()
    validate_contract(spec)
    return spec


def symbol_values() -> dict[str, str]:
    """Return every public contract symbol mapped to its concrete tag key."""
    spec = contract()
    entries = [*spec["tags"], *spec["aliases"], *spec["retired"], *spec["extension_tags"]]
    return {str(item["symbol"]): str(item["key"]) for item in entries}


def value(symbol: str) -> str:
    """Resolve one Python/TypeScript constant symbol from the contract."""
    try:
        return symbol_values()[symbol]
    except KeyError as exc:
        raise KeyError(f"unknown VGI tag symbol {symbol!r}") from exc
