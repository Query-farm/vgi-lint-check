"""VGI101x — worker-declared secret types (credentials).

A worker that needs credentials declares a secret type (name, description, and
an Arrow parameter schema) that the vgi extension registers at ATTACH, and its
functions declare which secret types they require. ``vgi_secret_types()``
surfaces those declarations; a listing turns them into a ready-to-fill
``CREATE SECRET`` template, a per-parameter table, "needed by" and a link to
where the credentials come from. These rules make sure that page can be built.

All of them stay silent on an extension without ``vgi_secret_types()``
(``Catalog.secret_types is None``): "declares nothing" and "can't tell" differ.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind, SecretType
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .catalog import _is_http_url
from .registry import register

AO = Category.ATTACH_OPTIONS

# A parameter whose name says it holds a credential. ``*_id`` names (client_id,
# access_key_id, tenant_id) are identifiers, not secrets, and are excluded.
_CREDENTIAL_NAME = re.compile(
    r"(secret|token|password|passwd|passphrase|api_?key|private_?key|credential|signing_?key)",
    re.IGNORECASE,
)


def _secret_types(ctx: RuleContext) -> list[SecretType]:
    return ctx.catalog.secret_types or []


@register
class SecretTypeDescription(Rule):
    code = "VGI1010"
    name = "secret-type-description"
    category = AO
    default_severity = Severity.WARNING
    targets = (ObjectKind.SECRET_TYPE,)
    summary = "A declared secret type should say what credentials it holds and for which service."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for st in _secret_types(ctx):
            d = st.description
            if blank(d) or is_trivial_echo(d, st.name) or len(str(d).strip()) < minlen:
                yield self.finding(
                    ctx,
                    st.id,
                    f"secret type {st.name!r} has no meaningful description",
                    "describe the service and credential kind (e.g. 'Microsoft Entra app-only "
                    "client credentials for Azure ARM / Microsoft Graph') — it heads the "
                    "credentials section of the worker's listing",
                )


@register
class SecretParameterDescription(Rule):
    code = "VGI1011"
    name = "secret-parameter-description"
    category = AO
    default_severity = Severity.WARNING
    targets = (ObjectKind.SECRET_TYPE,)
    summary = "Every secret-type parameter should carry a 'description' field-metadata entry."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for st in _secret_types(ctx):
            missing = [p.name for p in st.parameters if blank(p.description)]
            if missing:
                yield self.finding(
                    ctx,
                    st.id,
                    f"secret type {st.name!r} has undocumented parameter(s): {', '.join(missing)}",
                    "add a 'description' entry to each parameter field's Arrow metadata saying "
                    "what the value is and where to find it (e.g. 'Directory (tenant) ID from "
                    "the app registration's Overview page')",
                )


@register
class SecretTypeDocUrl(Rule):
    code = "VGI1012"
    name = "secret-type-doc-url"
    category = AO
    default_severity = Severity.INFO
    targets = (ObjectKind.SECRET_TYPE,)
    summary = "A secret type should link to how to obtain its credentials ('doc_url')."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for st in _secret_types(ctx):
            url = (st.doc_url or "").strip()
            if not url:
                yield self.finding(
                    ctx,
                    st.id,
                    f"secret type {st.name!r} has no 'doc_url'",
                    "set 'doc_url' in the parameter schema's Arrow metadata to the page that "
                    "explains how to get these credentials (sign-up, app registration, "
                    "required roles/scopes)",
                )
            elif not _is_http_url(url):
                yield self.finding(
                    ctx,
                    st.id,
                    f"secret type {st.name!r} 'doc_url' is not an http(s) URL: {url!r}",
                    "use an absolute http(s) URL",
                )


@register
class SecretParameterRedact(Rule):
    code = "VGI1013"
    name = "secret-parameter-redact"
    category = AO
    default_severity = Severity.WARNING
    targets = (ObjectKind.SECRET_TYPE,)
    summary = "A secret-type parameter that holds a credential must be marked redact."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for st in _secret_types(ctx):
            leaks = [
                p.name
                for p in st.parameters
                if not p.redact
                and _CREDENTIAL_NAME.search(p.name)
                and not p.name.lower().endswith("_id")
            ]
            if leaks:
                yield self.finding(
                    ctx,
                    st.id,
                    f"secret type {st.name!r} parameter(s) look like credentials but are not "
                    f"redacted: {', '.join(leaks)}",
                    "set field metadata redact='true' so SHOW SECRETS / duckdb_secrets() mask "
                    "the value",
                )
