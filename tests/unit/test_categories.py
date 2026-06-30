"""Tests for the vgi.category / vgi.categories navigation layer (VGI408-412)."""

import json

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.model import TagSet
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.tags import decode_categories

_CAT_CODES = {"VGI408", "VGI409", "VGI410", "VGI411", "VGI412"}


def _findings(cat):
    cfg = Config()
    return list(run(select_rules(cfg), RuleContext(cat, cfg)))


def _codes(cat):
    return {f.code for f in _findings(cat)}


def _reg(*entries):
    """Build a vgi.categories registry JSON from (name, description) pairs."""
    return json.dumps([{"name": n, "description": d} for n, d in entries])


# --- decoder ---------------------------------------------------------------
def test_decode_categories_valid_and_ordered():
    cats, err = decode_categories(TagSet({"vgi.categories": _reg(("b", "Bee"), ("a", "Ay"))}))
    assert err is None
    assert [c.name for c in cats] == ["b", "a"]  # registry order preserved


def test_decode_categories_duplicate_name():
    _cats, err = decode_categories(TagSet({"vgi.categories": '[{"name":"a"},{"name":"a"}]'}))
    assert err and "duplicate" in err


def test_decode_categories_missing_name():
    _cats, err = decode_categories(TagSet({"vgi.categories": '[{"description":"x"}]'}))
    assert err and "name" in err


# --- VGI408 registry validity / placement ----------------------------------
def test_vgi408_malformed_registry():
    s = F.schema("main", tags={"vgi.categories": "{not json"}, functions=[F.func("main", "f")])
    assert "VGI408" in _codes(F.catalog(s))


def test_vgi408_registry_on_catalog_rejected():
    cat = F.catalog(F.schema("main"))
    cat.tags.raw["vgi.categories"] = _reg(("a", "d"))
    msgs = [f.message for f in _findings(cat) if f.code == "VGI408"]
    assert any("not allowed on the catalog" in m for m in msgs)


def test_vgi408_category_on_schema_rejected():
    s = F.schema("main", tags={"vgi.category": "x"}, functions=[F.func("main", "f")])
    msgs = [f.message for f in _findings(F.catalog(s)) if f.code == "VGI408"]
    assert any("not allowed on a schema" in m for m in msgs)


# --- VGI409 referential integrity ------------------------------------------
def test_vgi409_orphan_with_did_you_mean():
    fn = F.func("main", "easter", tags={"vgi.category": "holdays"})  # typo
    s = F.schema(
        "main", tags={"vgi.categories": _reg(("holidays", "Public holidays"))}, functions=[fn]
    )
    msgs = [f.message for f in _findings(F.catalog(s)) if f.code == "VGI409"]
    assert msgs and "did you mean 'holidays'" in msgs[0]


def test_vgi409_category_without_registry():
    fn = F.func("main", "easter", tags={"vgi.category": "holidays"})
    s = F.schema("main", functions=[fn])  # opts in via the object, but no registry
    msgs = [f.message for f in _findings(F.catalog(s)) if f.code == "VGI409"]
    assert any("declares no vgi.categories registry" in m for m in msgs)


def test_vgi409_category_must_be_single_string():
    fn = F.func("main", "easter", tags={"vgi.category": '["a","b"]'})
    s = F.schema("main", tags={"vgi.categories": _reg(("a", "d"))}, functions=[fn])
    msgs = [f.message for f in _findings(F.catalog(s)) if f.code == "VGI409"]
    assert any("single category name" in m for m in msgs)


def test_vgi409_valid_category_passes():
    fn = F.func("main", "easter", tags={"vgi.category": "holidays"})
    s = F.schema(
        "main", tags={"vgi.categories": _reg(("holidays", "Public holidays"))}, functions=[fn]
    )
    assert "VGI409" not in _codes(F.catalog(s))


# --- VGI410 described ------------------------------------------------------
def test_vgi410_category_without_description():
    fn = F.func("main", "easter", tags={"vgi.category": "a"})
    s = F.schema("main", tags={"vgi.categories": '[{"name":"a"}]'}, functions=[fn])
    assert "VGI410" in _codes(F.catalog(s))


# --- VGI411 coverage -------------------------------------------------------
def test_vgi411_uncategorized_object_in_registry_schema():
    filed = F.func("main", "easter", tags={"vgi.category": "holidays"})
    unfiled = F.func("main", "iso_week")
    s = F.schema(
        "main", tags={"vgi.categories": _reg(("holidays", "d"))}, functions=[filed, unfiled]
    )
    msgs = [(f.object_id.name, f.message) for f in _findings(F.catalog(s)) if f.code == "VGI411"]
    assert any(name == "iso_week" for name, _ in msgs)
    assert all(name != "easter" for name, _ in msgs)


# --- VGI412 empty category (now an ERROR) ----------------------------------
def test_vgi412_empty_category_is_error():
    fn = F.func("main", "easter", tags={"vgi.category": "holidays"})
    s = F.schema(
        "main",
        tags={"vgi.categories": _reg(("holidays", "d"), ("trading", "d2"))},
        functions=[fn],
    )
    f412 = [f for f in _findings(F.catalog(s)) if f.code == "VGI412"]
    assert f412 and any("'trading'" in f.message for f in f412)
    assert f412[0].severity.name == "ERROR"


# --- VGI413 categories registry is required --------------------------------
def test_vgi413_schema_without_registry_flagged():
    # A schema with objects but no vgi.categories registry -> required (VGI413).
    s = F.schema("main", comment="c", functions=[F.func("main", "easter", description="d")])
    assert "VGI413" in _codes(F.catalog(s))


def test_vgi413_satisfied_by_full_registry():
    fn = F.func("main", "easter", description="d", tags={"vgi.category": "holidays"})
    s = F.schema(
        "main",
        comment="c",
        tags={"vgi.categories": _reg(("holidays", "Public holidays"))},
        functions=[fn],
    )
    assert _CAT_CODES.isdisjoint(_codes(F.catalog(s)))  # fully categorized -> clean


def test_vgi413_not_fired_on_objectless_schema():
    s = F.schema("main", comment="c")  # nothing to categorize
    assert "VGI413" not in _codes(F.catalog(s))
