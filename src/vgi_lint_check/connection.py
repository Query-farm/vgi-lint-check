"""haybarn connection + VGI worker attach lifecycle.

The only module that talks to a worker. We open one haybarn connection, load the
``vgi`` extension once, then ATTACH/DETACH per data version on that same
connection. ``attached()`` is a context manager that always DETACHes — so
subprocess workers are reaped and a failed lint never leaves a half-attached
catalog behind.

The ATTACH *name* must be the worker's catalog name (discovered via
``vgi_catalogs``), distinct from the local ``alias`` handle.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
log = logging.getLogger("vgi_lint_check")


class WorkerConnectionError(RuntimeError):
    """Operational failure reaching/attaching a worker (CLI exit code 3)."""


def sql_str(value: str) -> str:
    """Quote a value as a DuckDB single-quoted string literal (injection-safe)."""
    return "'" + str(value).replace("'", "''") + "'"


def validate_alias(alias: str) -> str:
    """Return ``alias`` unchanged if it is a safe SQL identifier, else raise."""
    if not ALIAS_RE.match(alias or ""):
        raise ValueError(f"invalid catalog alias {alias!r}; must match {ALIAS_RE.pattern}")
    return alias


def derive_alias(catalog_name: str) -> str:
    """Make a safe local alias from a catalog name."""
    slug = re.sub(r"[^A-Za-z0-9_]", "_", catalog_name or "worker")
    if not slug or not re.match(r"[A-Za-z_]", slug[0]):
        slug = "w_" + slug
    return slug


def connect_loaded(*, install: bool = True, spatial: bool = False) -> tuple[Any, str | None]:
    """Open a haybarn connection with the vgi extension loaded.

    Returns ``(con, vgi_version)``.
    """
    import haybarn

    con = haybarn.connect()
    if install:
        try:
            # FORCE INSTALL re-fetches the community build so we always lint
            # against the current vgi extension, not a stale cached copy.
            con.execute("FORCE INSTALL vgi FROM community")
        except Exception as e:  # noqa: BLE001
            con.close()
            raise WorkerConnectionError(
                "couldn't FORCE INSTALL vgi FROM community — offline or community "
                f"repo blocked? preinstall and use --no-install. ({e})"
            ) from e
    try:
        con.execute("LOAD vgi")
    except Exception as e:  # noqa: BLE001
        con.close()
        raise WorkerConnectionError(f"couldn't LOAD the vgi extension: {e}") from e
    if spatial:
        try:
            if install:
                con.execute("INSTALL spatial")
            con.execute("LOAD spatial")
        except Exception as e:  # noqa: BLE001
            log.warning("spatial extension unavailable; continuing (%s)", e)
    return con, _vgi_version(con)


def _vgi_version(con: Any) -> str | None:
    try:
        row = con.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'vgi'"
        ).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 - best-effort metadata
        return None


def attach_statement(location: str, catalog_name: str, alias: str, data_version: str | None) -> str:
    """Build the ``ATTACH ... (TYPE vgi, ...)`` statement for a worker catalog."""
    validate_alias(alias)
    dv = f", data_version_spec {sql_str(data_version)}" if data_version else ""
    return f"ATTACH {sql_str(catalog_name)} AS {alias} (TYPE vgi, LOCATION {sql_str(location)}{dv})"


@contextmanager
def attached(
    con: Any,
    location: str,
    catalog_name: str,
    alias: str,
    *,
    data_version: str | None = None,
) -> Iterator[Any]:
    """ATTACH a catalog on ``con`` for the body, DETACH on exit."""
    try:
        con.execute(attach_statement(location, catalog_name, alias, data_version))
    except Exception as e:  # noqa: BLE001
        raise WorkerConnectionError(_explain_attach_failure(location, data_version, e)) from e
    try:
        yield con
    finally:
        # Best-effort cleanup; a detach failure must not mask the real result.
        with contextlib.suppress(Exception):
            con.execute(f"DETACH {alias}")


def read_default_schema(con: Any, alias: str) -> str | None:
    """Return the worker's declared default schema (via current_schema()).

    Switches the session to the catalog to read its default, then restores the
    prior current catalog/schema. Returns None if it can't be determined.
    """
    try:
        prev_db = con.execute("SELECT current_database()").fetchone()[0]
        prev_schema = con.execute("SELECT current_schema()").fetchone()[0]
        con.execute(f"USE {alias}")
        schema = con.execute("SELECT current_schema()").fetchone()[0]
    except Exception:  # noqa: BLE001 - best-effort metadata
        return None
    finally:
        with contextlib.suppress(Exception):
            con.execute(f'USE "{prev_db}"."{prev_schema}"')
    return str(schema) if schema is not None else None


def _explain_attach_failure(location: str, data_version: str | None, e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "authentication" in low or "401" in low or "oauth" in low:
        return (
            f"worker at {location!r} requires authentication, which this version "
            f"does not support (no-auth workers only). ({msg})"
        )
    if "unsupported data_version" in low or (data_version and "data_version" in low):
        return f"worker at {location!r} does not serve data version {data_version!r}. ({msg})"
    if any(s in low for s in ("connection", "refused", "could not", "io error")):
        return (
            f"couldn't reach {location!r} — is the worker running? For a local "
            f"worker use a subprocess LOCATION, e.g. "
            f"vgi-lint 'uv run volcano_worker.py'. ({msg})"
        )
    return f"failed to attach worker at {location!r}: {msg}"
