"""VGI10xx — attach-option documentation.

A worker advertises its attach-time options through ``vgi_catalogs()`` *before*
attach (name, description, type, default). An agent picking the worker relies on
those descriptions to know what each option does and which are required (implied
by the absence of a default), so they should be documented like any other API.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..corpus import _iter_doc_sql, _iter_exec_sql, _key
from ..findings import Category, Finding, Severity
from ..model import Function, ObjectKind
from ..sql_parse import call_constant_args
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

AO = Category.ATTACH_OPTIONS


@register
class AttachOptionDescription(Rule):
    code = "VGI1001"
    name = "attach-option-description"
    category = AO
    default_severity = Severity.WARNING
    targets = (ObjectKind.ATTACH_OPTION,)
    summary = "Every advertised attach option should have a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for opt in ctx.catalog.iter_attach_options():
            if blank(opt.description):
                req = " (required — it has no default)" if opt.required else ""
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach option {opt.name!r} has no description{req}",
                    "describe what the option controls and its accepted values so "
                    "callers know how to attach the worker",
                )


@register
class AttachOptionDescriptionQuality(Rule):
    code = "VGI1002"
    name = "attach-option-description-quality"
    category = AO
    default_severity = Severity.INFO
    targets = (ObjectKind.ATTACH_OPTION,)
    summary = "An attach-option description should add information, not restate the name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for opt in ctx.catalog.iter_attach_options():
            d = opt.description
            if blank(d):
                continue
            if is_trivial_echo(d, opt.name):
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach-option description just restates {opt.name!r}",
                    "describe what the option does, not just its name",
                )
            elif len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach-option description is very short "
                    f"({len((d or '').strip())} < {minlen})",
                    "expand the description so callers understand the option",
                )


# Examples legitimately repeat sample *inputs* (a demo certificate, a SMILES
# string, a fixture path), so only values that look like connection config
# count: an endpoint URL, or an argument whose name says it is config.
_ENDPOINT_VALUE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$", re.IGNORECASE)
_CONFIG_ARG_NAME = re.compile(
    r"^(?:.*_)?(?:url|uri|endpoint|host|hostname|server|cluster|base_url|api_base|"
    r"region|location|account|tenant|tenant_id|subscription|subscription_id|project|"
    r"workspace|organization|org|provider|model|database|instance|domain)$",
    re.IGNORECASE,
)


def _looks_like_config(label: str, value: str) -> bool:
    return bool(_ENDPOINT_VALUE.match(value.strip()) or _CONFIG_ARG_NAME.match(label))


def _arg_label(fn: Function, key: str) -> str:
    """Human name for a call-argument key (``#0`` -> the declared positional name)."""
    if not key.startswith("#"):
        return key
    pos = int(key[1:])
    # Order by ordinal rather than trusting its base (0- vs 1-based).
    positional = sorted(
        (a for a in fn.arguments if a.position is not None), key=lambda a: a.position or 0
    )
    if pos < len(positional):
        return positional[pos].name
    if pos < len(fn.parameters):
        return fn.parameters[pos]
    return f"argument {pos + 1}"


@register
class ConstantArgumentAttachOption(Rule):
    code = "VGI1003"
    name = "constant-argument-attach-option"
    category = AO
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE_FUNCTION, ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE)
    summary = (
        "An endpoint/config-like argument every example passes with the same value "
        "should probably be an attach option."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        min_calls = ctx.config.options.constant_argument_min_calls
        if min_calls <= 0:
            return
        default_schema = (cat.default_schema or "main").lower()
        qualifier = cat.qualifier.lower()
        fns = {
            _key(f.schema, f.name, default_schema): f
            for f in cat.iter_all_functions()
            if f.id.kind in self.targets
        }
        if not fns:
            return
        # key -> list of {arg key: literal} for every call, across distinct SQL.
        calls: dict[str, list[dict[str, str]]] = {}
        seen_sql: set[str] = set()
        for sql, _origin, _where in [*_iter_doc_sql(cat), *_iter_exec_sql(cat)]:
            norm = " ".join(sql.split())
            if norm in seen_sql:
                continue
            seen_sql.add(norm)
            for call in call_constant_args(sql) or []:
                if call.catalog and call.catalog.lower() != qualifier:
                    continue
                key = _key(call.schema, call.name, default_schema)
                if key not in fns:
                    continue
                strings = {k: v for k, v in call.constants.items() if call.kinds.get(k) == "STRING"}
                calls.setdefault(key, []).append(strings)
        for key, per_call in calls.items():
            if len(per_call) < min_calls:
                continue
            fn = fns[key]
            shared = dict(per_call[0])
            for c in per_call[1:]:
                shared = {k: v for k, v in shared.items() if c.get(k) == v}
            for arg_key, value in sorted(shared.items()):
                if not value.strip():
                    continue
                label = _arg_label(fn, arg_key)
                if not _looks_like_config(label, value):
                    continue
                yield self.finding(
                    ctx,
                    fn.id,
                    f"every example ({len(per_call)} calls) passes {fn.name}"
                    f"({label} => {value!r}) — the same value each time",
                    f"if {label!r} is per-connection configuration (an endpoint, region, "
                    "account, default provider/model), declare it as an attach option so "
                    "users set it once at ATTACH; otherwise vary it across examples to show "
                    "its range",
                )
