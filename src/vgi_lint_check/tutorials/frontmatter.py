"""YAML front-matter splitting and coercion.

``split_frontmatter`` separates the leading ``---`` fenced YAML block from the
Markdown body. ``coerce_frontmatter`` turns the parsed mapping into a typed
:class:`TutorialFrontMatter`, normalizing the ``worker``/``attach`` shorthands
and the ``assets`` list. Neither raises: YAML/shape problems are returned as a
list of error strings.
"""

from __future__ import annotations

from datetime import date, datetime

import yaml

from .model import AttachSpec, TutorialAsset, TutorialFrontMatter

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[dict[str, object] | None, str, str | None]:
    """Split ``text`` into ``(mapping, body, error)``.

    Args:
        text: The full ``.vgi.md`` file contents.

    Returns:
        ``(mapping, body, error)``. ``mapping`` is the parsed YAML mapping (or
        ``None`` when absent/invalid), ``body`` is the Markdown after the
        front-matter, and ``error`` describes a missing or malformed block.
    """
    stripped = text.lstrip("﻿")
    if not stripped.startswith(_FENCE):
        return None, text, "missing YAML front-matter (file must start with '---')"

    # Find the closing fence on its own line.
    rest = stripped[len(_FENCE) :]
    if not rest.startswith("\n"):
        return None, text, "malformed front-matter opening fence"
    end = rest.find(f"\n{_FENCE}")
    if end == -1:
        return None, text, "unterminated YAML front-matter (no closing '---')"

    yaml_src = rest[:end]
    body = rest[end + len(_FENCE) + 1 :]
    body = body.lstrip("\n")
    try:
        data = yaml.safe_load(yaml_src)
    except yaml.YAMLError as e:
        return None, body, f"invalid YAML front-matter: {e}"
    if data is None:
        return {}, body, None
    if not isinstance(data, dict):
        return None, body, f"front-matter must be a mapping, got {type(data).__name__}"
    return data, body, None


def coerce_frontmatter(mapping: dict[str, object]) -> tuple[TutorialFrontMatter, list[str]]:
    """Coerce a raw front-matter mapping into typed fields.

    Args:
        mapping: The parsed YAML mapping from :func:`split_frontmatter`.

    Returns:
        A ``(front_matter, errors)`` tuple. ``errors`` lists per-field problems
        (bad types, unparseable shorthands); it is empty when every present
        field is well-formed. Missing-required-field checks are a linter
        concern, not a loader one.
    """
    errors: list[str] = []

    def _str(key: str) -> str | None:
        val = mapping.get(key)
        if val is None:
            return None
        if not isinstance(val, str):
            errors.append(f"{key!r} must be a string, got {type(val).__name__}")
            return None
        return val

    def _date_str(key: str) -> str | None:
        # YAML auto-parses unquoted ISO dates to date/datetime; accept both.
        val = mapping.get(key)
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date().isoformat()
        if isinstance(val, date):
            return val.isoformat()
        if isinstance(val, str):
            return val
        errors.append(f"{key!r} must be a date, got {type(val).__name__}")
        return None

    def _str_list(key: str) -> list[str]:
        val = mapping.get(key)
        if val is None:
            return []
        if isinstance(val, str):
            return [val]
        if isinstance(val, list) and all(isinstance(x, str) for x in val):
            return list(val)
        errors.append(f"{key!r} must be a string or list of strings")
        return []

    est: int | None = None
    raw_est = mapping.get("est_minutes")
    if raw_est is not None:
        if isinstance(raw_est, bool) or not isinstance(raw_est, int):
            errors.append("'est_minutes' must be an integer")
        else:
            est = raw_est

    attach, workers = _coerce_attach(mapping, errors)
    assets = _coerce_assets(mapping, errors)
    runtime = mapping.get("runtime")
    if runtime is not None and not isinstance(runtime, dict):
        errors.append("'runtime' must be a mapping")
        runtime = {}

    fm = TutorialFrontMatter(
        title=_str("title"),
        workers=workers,
        description=_str("description"),
        slug=_str("slug"),
        keywords=_str_list("keywords"),
        difficulty=_str("difficulty"),
        est_minutes=est,
        dataset=mapping.get("dataset"),
        date_published=_date_str("datePublished"),
        date_modified=_date_str("dateModified"),
        tier=_str("tier"),
        attach=attach,
        runtime=dict(runtime) if isinstance(runtime, dict) else {},
        assets=assets,
        raw=dict(mapping),
    )
    return fm, errors


def _coerce_attach(
    mapping: dict[str, object], errors: list[str]
) -> tuple[list[AttachSpec], list[str]]:
    """Normalize the ``worker``/``data_version`` shorthand and ``attach`` list."""
    specs: list[AttachSpec] = []
    raw_attach = mapping.get("attach")
    if raw_attach is not None:
        if not isinstance(raw_attach, list):
            errors.append("'attach' must be a list of {worker, data_version?, as?} mappings")
        else:
            for i, item in enumerate(raw_attach):
                if not isinstance(item, dict) or not isinstance(item.get("worker"), str):
                    errors.append(f"attach[{i}] must have a string 'worker'")
                    continue
                specs.append(
                    AttachSpec(
                        worker=item["worker"],
                        data_version=_opt_str(item.get("data_version")),
                        alias=_opt_str(item.get("as")),
                    )
                )
    worker = mapping.get("worker")
    if isinstance(worker, str):
        specs.append(AttachSpec(worker=worker, data_version=_opt_str(mapping.get("data_version"))))
    elif worker is not None:
        errors.append("'worker' must be a string")

    workers = [s.worker for s in specs]
    return specs, workers


def _coerce_assets(mapping: dict[str, object], errors: list[str]) -> list[TutorialAsset]:
    """Normalize the front-matter ``assets`` list."""
    raw = mapping.get("assets")
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("'assets' must be a list of {path, kind, ...} mappings")
        return []
    assets: list[TutorialAsset] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append(f"assets[{i}] must have a string 'path'")
            continue
        assets.append(
            TutorialAsset(
                path=item["path"],
                kind=_opt_str(item.get("kind")) or "data",
                alt=_opt_str(item.get("alt")),
                provenance=_opt_str(item.get("provenance")),
                sha256=_opt_str(item.get("sha256")),
            )
        )
    return assets


def _opt_str(value: object) -> str | None:
    """Coerce an optional scalar to ``str`` (``None`` stays ``None``)."""
    return None if value is None else str(value)
