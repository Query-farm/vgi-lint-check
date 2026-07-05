"""Tests for VGI317 (constrained-argument-not-discoverable) and the simulate render."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.simulate import tool_describe_function


def _codes(cat, **kw):
    cfg = Config(**kw)
    return {f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))}


def _messages(cat, code, **kw):
    cfg = Config(**kw)
    return [f.message for f in run(select_rules(cfg), RuleContext(cat, cfg)) if f.code == code]


# --- VGI317 fires on prose enumeration / range without machine constraints ---
def test_vgi317_flags_enumerated_values():
    f = F.func(
        "main",
        "resize",
        description="d",
        arguments=[F.arg("mode", "VARCHAR", "one of nearest, linear, cubic")],
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI317")
    assert msgs and "mode" in msgs[0]


def test_vgi317_flags_quoted_list():
    f = F.func(
        "main",
        "fmt",
        description="d",
        arguments=[F.arg("unit", "VARCHAR", "the unit: 'mm', 'cm', or 'm'")],
    )
    assert "VGI317" in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi317_flags_numeric_range():
    f = F.func(
        "main",
        "clamp",
        description="d",
        arguments=[F.arg("pct", "DOUBLE", "a percentage between 0 and 100")],
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI317")
    assert msgs and "numeric range" in msgs[0]


# --- VGI317 stays quiet once the constraint is machine-readable --------------
def test_vgi317_silent_when_choices_declared():
    f = F.func(
        "main",
        "resize",
        description="d",
        arguments=[
            F.arg(
                "mode",
                "VARCHAR",
                "one of nearest, linear, cubic",
                choices='["nearest", "linear", "cubic"]',
            )
        ],
    )
    assert "VGI317" not in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi317_silent_when_range_declared():
    f = F.func(
        "main",
        "clamp",
        description="d",
        arguments=[
            F.arg("pct", "DOUBLE", "a percentage between 0 and 100", value_range="[0, 100]")
        ],
    )
    assert "VGI317" not in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi317_silent_on_plain_description():
    f = F.func(
        "main",
        "scale",
        description="d",
        arguments=[F.arg("factor", "DOUBLE", "the multiplication factor")],
    )
    assert "VGI317" not in _codes(F.catalog(F.schema("main", functions=[f])))


# --- simulate: tool_describe_function surfaces the constraints ---------------
def test_describe_function_surfaces_constraints():
    f = F.func(
        "main",
        "format_measure",
        description="Format a measurement",
        arguments=[
            F.arg("unit", "VARCHAR", "output unit", choices='["mm", "cm", "m"]', default='"mm"'),
            F.arg("precision", "BIGINT", "decimal places", value_range="[0, 10]"),
            F.arg("code", "VARCHAR", "label code", pattern="^[A-Z]{2}$"),
            F.arg("value", "DOUBLE", "measurement"),
        ],
    )
    cat = F.catalog(F.schema("main", functions=[f]))
    out = tool_describe_function(cat, "main", "format_measure")
    by_name = {a["name"]: a for a in out["arguments"]}
    # choices/default JSON is decoded to native values for the analyst.
    assert by_name["unit"]["allowed_values"] == ["mm", "cm", "m"]
    assert by_name["unit"]["default"] == "mm"
    assert by_name["precision"]["range"] == "[0, 10]"
    assert by_name["code"]["pattern"] == "^[A-Z]{2}$"
    # An unconstrained arg carries none of the constraint keys.
    assert "allowed_values" not in by_name["value"]
    assert "range" not in by_name["value"]


# --- VGI318 default-violates-constraint (ERROR) -----------------------------
def test_vgi318_default_not_in_choices():
    f = F.func(
        "main",
        "fmt",
        description="d",
        arguments=[F.arg("unit", "VARCHAR", "u", default='"xx"', choices='["mm", "cm", "m"]')],
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI318")
    assert msgs and "not one of" in msgs[0]


def test_vgi318_default_in_choices_clean():
    f = F.func(
        "main",
        "fmt",
        description="d",
        arguments=[F.arg("unit", "VARCHAR", "u", default='"cm"', choices='["mm", "cm", "m"]')],
    )
    assert "VGI318" not in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi318_default_out_of_range():
    f = F.func(
        "main",
        "clamp",
        description="d",
        arguments=[F.arg("p", "BIGINT", "p", default="99", value_range="[0, 10]")],
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI318")
    assert msgs and "range" in msgs[0]


def test_vgi318_default_in_range_clean():
    f = F.func(
        "main",
        "clamp",
        description="d",
        arguments=[F.arg("p", "BIGINT", "p", default="5", value_range="[0, 10]")],
    )
    assert "VGI318" not in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi318_default_fails_pattern():
    f = F.func(
        "main",
        "code",
        description="d",
        arguments=[F.arg("c", "VARCHAR", "c", default='"abc"', pattern="^[A-Z]{2}$")],
    )
    assert "VGI318" in _codes(F.catalog(F.schema("main", functions=[f])))


def test_vgi318_null_default_ignored():
    f = F.func(
        "main",
        "fmt",
        description="d",
        arguments=[F.arg("u", "VARCHAR", "u", default="null", choices='["a", "b"]')],
    )
    assert "VGI318" not in _codes(F.catalog(F.schema("main", functions=[f])))


# --- VGI319 invalid-constraint (WARNING) ------------------------------------
def test_vgi319_invalid_regex():
    f = F.func(
        "main", "m", description="d", arguments=[F.arg("x", "VARCHAR", "x", pattern="[unclosed")]
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI319")
    assert msgs and "regex" in msgs[0]


def test_vgi319_empty_range():
    f = F.func(
        "main", "m", description="d", arguments=[F.arg("y", "BIGINT", "y", value_range="[10, 0]")]
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI319")
    assert msgs and "empty" in msgs[0]


def test_vgi319_valid_constraints_clean():
    f = F.func(
        "main",
        "m",
        description="d",
        arguments=[
            F.arg("x", "VARCHAR", "x", pattern="^[a-z]+$"),
            F.arg("y", "BIGINT", "y", value_range="[0, 10]"),
        ],
    )
    assert "VGI319" not in _codes(F.catalog(F.schema("main", functions=[f])))


# --- VGI320 degenerate-choices (INFO) ---------------------------------------
def test_vgi320_single_choice():
    f = F.func(
        "main", "m", description="d", arguments=[F.arg("z", "VARCHAR", "z", choices='["only"]')]
    )
    msgs = _messages(F.catalog(F.schema("main", functions=[f])), "VGI320")
    assert msgs and "single-value" in msgs[0]


def test_vgi320_two_choices_clean():
    f = F.func(
        "main", "m", description="d", arguments=[F.arg("z", "VARCHAR", "z", choices='["a", "b"]')]
    )
    assert "VGI320" not in _codes(F.catalog(F.schema("main", functions=[f])))
