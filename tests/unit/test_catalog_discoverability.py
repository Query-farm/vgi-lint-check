"""Tests for the catalog (VGI0xx) and discoverability (VGI12x) rules."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.model import Release
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def codes(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


# --- catalog required (VGI0xx) --------------------------------------------
def test_catalog_metadata_required_fires_when_missing():
    cat = F.catalog(F.schema("main"), comment=None, tags={}, source_url=None)
    found = set(codes(cat))
    assert {"VGI001", "VGI002", "VGI003", "VGI004"} <= found


def test_catalog_metadata_passes_when_present():
    # F.catalog() defaults supply comment/llm/md/source_url
    cat = F.catalog(F.schema("main", comment="A schema describing main test data"))
    found = set(codes(cat))
    assert not (found & {"VGI001", "VGI002", "VGI003", "VGI004"})


# --- data-version semver validity (VGI005/006/007) -----------------------
def test_data_version_spec_and_releases_valid():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0"), Release(version="1.5.0")],
    )
    cat.data_version_spec = ">=1.0.0,<2.0.0"
    found = set(codes(cat))
    assert not (found & {"VGI005", "VGI006", "VGI007"})


def test_invalid_data_version_spec_flagged():
    cat = F.catalog(F.schema("main"))
    cat.data_version_spec = "not a spec"
    assert "VGI005" in set(codes(cat))


def test_invalid_release_version_flagged():
    cat = F.catalog(F.schema("main"), releases=[Release(version="v1.2.three")])
    assert "VGI006" in set(codes(cat))


def test_release_outside_spec_flagged():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0"), Release(version="2.5.0")],
    )
    cat.data_version_spec = ">=1.0.0,<2.0.0"
    found = set(codes(cat))
    assert "VGI007" in found  # 2.5.0 is outside >=1.0.0,<2.0.0
    assert "VGI005" not in found  # the spec itself is valid


# --- discoverability (VGI12x) ---------------------------------------------
def test_duplicate_descriptions_flagged():
    t1 = F.table("main", "a", comment="Reference data about the system overall")
    t2 = F.table("main", "b", comment="Reference data about the system overall")
    cat = F.catalog(F.schema("main", tables=[t1, t2]))
    assert "VGI120" in set(codes(cat))


def test_duplicate_descriptions_span_schemas_and_functions():
    dup = "Reference data describing the overall system and its parts"
    f1 = F.func("main", "f1", "scalar", description=dup)
    f2 = F.func("main", "f2", "scalar", description=dup)
    s = F.schema("main", comment=dup, functions=[f1, f2])
    cfg = Config()
    findings = [
        f for f in run(select_rules(cfg), RuleContext(F.catalog(s), cfg)) if f.code == "VGI120"
    ]
    flagged = {f.object_id.qualified() for f in findings}
    # schema + both functions all flagged as sharing one description
    assert {"v.main", "v.main.f1", "v.main.f2"} <= flagged
    assert findings[0].severity is Severity.WARNING


def test_short_and_echo_descriptions():
    short = F.table("main", "metrics", comment="metrics")  # short + echo
    cat = F.catalog(F.schema("main", tables=[short]))
    found = set(codes(cat))
    assert "VGI121" in found  # too short
    assert "VGI122" in found  # echoes name


# --- catalog description substance (VGI106 + VGI121/122 on the catalog) ----
def test_single_word_catalog_description_is_an_error():
    cat = F.catalog(F.schema("main"), comment="cbor")
    findings = [f for f in run(select_rules(Config()), RuleContext(cat, Config()))]
    vgi106 = [f for f in findings if f.code == "VGI106"]
    assert len(vgi106) == 1
    assert vgi106[0].severity is Severity.ERROR
    assert "single word" in vgi106[0].message
    # the catalog is now in scope for the general description rules too
    assert "VGI121" in {f.code for f in findings}


def test_catalog_description_echoing_the_name_is_flagged():
    cat = F.catalog(F.schema("main"), comment="v")  # F.catalog() uses database="v"
    found = set(codes(cat))
    assert "VGI106" in found
    assert "VGI122" in found  # echoes the catalog name


def test_short_but_multiword_catalog_description_still_flagged():
    cat = F.catalog(F.schema("main"), comment="Fixed-width codec.")
    found = set(codes(cat))
    assert "VGI106" in found


def test_descriptive_catalog_description_passes():
    cat = F.catalog(
        F.schema("main"),
        comment="CBOR (RFC 8949) / MessagePack decode & encode for SQL.",
    )
    found = set(codes(cat))
    assert "VGI106" not in found
    assert "VGI121" not in found
    assert "VGI122" not in found


def test_missing_catalog_description_is_not_vgi106():
    # Absence is VGI001's business; VGI106 only grades what is present.
    cat = F.catalog(F.schema("main"), comment=None)
    found = set(codes(cat))
    assert "VGI106" not in found
    assert "VGI001" in found


def test_trivial_examples_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table with only a trivial example query",
        examples=[F.example(0, "all", "SELECT * FROM v.main.t")],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI150" in set(codes(cat))


def test_minimum_examples_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table that ships too few example queries",
        examples=[F.example(0, "x", "SELECT name FROM v.main.t WHERE id=1")],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI151" in set(codes(cat))  # 1 < default min 3


def test_minimum_examples_counts_table_functions():
    # A table-function-only worker (e.g. a model-registry worker) keeps all its
    # examples on table-functions: excluded from iter_functions() and with no
    # materialized table rows. VGI151 must still count them (via
    # iter_all_functions()), so a worker shipping >= min examples does not fire.
    fns = [
        F.func(
            "main",
            f"f{i}",
            ftype="table",
            description="A table function that ships an example",
            examples=[F.example(0, "demo", f"SELECT * FROM v.main.f{i}()")],
        )
        for i in range(3)
    ]
    cat = F.catalog(F.schema("main", functions=fns))
    assert "VGI151" not in set(codes(cat))  # 3 table-function examples >= min 3


def test_release_freshness_rules():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0", released_at=None, summary="", notes_url=None)],
    )
    found = set(codes(cat))
    assert "VGI140" in found  # no released_at
    assert "VGI141" in found  # no summary/notes_url


def test_title_keywords_present_strict_default_and_quality():
    # presence rules (VGI124 title, VGI126 keywords) are on under the strict default
    t = F.table("main", "t", comment="A table for testing title/keyword conventions")
    cat = F.catalog(F.schema("main", tables=[t]))
    base = set(codes(cat))
    assert "VGI124" in base and "VGI126" in base
    # VGI124 (title) is required only on the catalog + schemas, not on tables
    cfg = Config()
    title_objs = {
        f.object_id.kind
        for f in run(select_rules(cfg), RuleContext(cat, cfg))
        if f.code == "VGI124"
    }
    assert title_objs == {"catalog", "schema"}

    # quality rules fire when the tags ARE set badly
    bad = F.table(
        "main",
        "metrics",
        comment="A metrics table for testing tag quality checks",
        tags={"vgi.title": "metrics", "vgi.keywords": "a, a, b"},  # echo + duplicate
    )
    cat2 = F.catalog(F.schema("main", tables=[bad]))
    found = set(codes(cat2))
    assert "VGI125" in found  # title echoes the name
    assert "VGI127" in found  # duplicate keywords


def test_source_url_present_opt_in_and_valid():
    bad = F.table(
        "main",
        "t",
        comment="A table whose source link is not a real URL here",
        tags={"vgi.source_url": "see the repo"},  # not http(s)
    )
    cat = F.catalog(F.schema("main", tables=[bad]))
    found = set(codes(cat))
    assert "VGI129" in found  # invalid URL flagged when present


def test_catalog_support_tags():
    # F.catalog defaults supply support contact + policy url -> no finding
    assert "VGI009" not in set(codes(F.catalog(F.schema("main"))))
    bare = F.catalog(
        F.schema("main"),
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
    )
    assert "VGI009" in set(codes(bare))


def test_support_contact_and_policy_url_validity():
    # a URL-shaped support contact that isn't http(s) is flagged
    assert "VGI010" in set(
        codes(F.catalog(F.schema("main"), tags={"vgi.support_contact": "ftp://x"}))
    )
    # an email contact is fine (no URL to validate)
    assert "VGI010" not in set(
        codes(F.catalog(F.schema("main"), tags={"vgi.support_contact": "help@example.com"}))
    )
    # a valid http(s) contact URL is fine
    assert "VGI010" not in set(
        codes(
            F.catalog(F.schema("main"), tags={"vgi.support_contact": "https://example.com/issues"})
        )
    )
    # a support policy URL must be a real http(s) URL
    assert "VGI010" in set(
        codes(F.catalog(F.schema("main"), tags={"vgi.support_policy_url": "example.com/policy"}))
    )


def test_catalog_attribution_required_tags():
    # F.catalog defaults supply author/copyright/license -> no finding
    assert "VGI160" not in set(codes(F.catalog(F.schema("main"))))
    bare = F.catalog(
        F.schema("main"),
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
    )
    assert "VGI160" in set(codes(bare))


def test_classifying_tag_and_units_strict_default():
    untagged = F.table(
        "main",
        "t",
        comment="A table with no classifying tags at all",
        columns=[F.col("main", "t", "depth", "the depth value", "INTEGER")],
    )
    cat = F.catalog(F.schema("main", tables=[untagged]))
    # on under the strict default
    assert "VGI123" in set(codes(cat))
    assert "VGI131" in set(codes(cat))  # numeric 'depth' comment has no unit
    # ...and can be turned off
    off = codes(cat, severity_overrides={"VGI123": Severity.OFF, "VGI131": Severity.OFF})
    assert "VGI123" not in set(off)
    assert "VGI131" not in set(off)


# --- VGI014 / VGI015 catalog icon (static shape + network image) ----------
def test_icon_url_shape_opt_in_and_valid():
    # absent -> nothing (icon is opt-in)
    assert "VGI014" not in set(codes(F.catalog(F.schema("main"))))
    # present + malformed -> flagged
    assert "VGI014" in set(codes(F.catalog(F.schema("main"), tags={"vgi.icon_url": "logo.png"})))
    # present + valid http(s) -> not flagged
    assert "VGI014" not in set(
        codes(F.catalog(F.schema("main"), tags={"vgi.icon_url": "https://example.com/logo.png"}))
    )


def _run_icon(cat, probe, *, check_links=True):
    from vgi_lint_check.findings import Severity
    from vgi_lint_check.linkcheck import ImageInfo  # noqa: F401
    from vgi_lint_check.rules.catalog import CatalogIconImage

    cfg = Config(check_links=check_links)
    ctx = RuleContext(cat, cfg, image_probe=probe)
    ctx.severity = Severity.WARNING
    return list(CatalogIconImage().check(ctx))


def _icon_cat(url="https://example.com/logo.png"):
    return F.catalog(F.schema("main"), tags={"vgi.icon_url": url})


def test_icon_image_accepts_good_image():
    from vgi_lint_check.linkcheck import ImageInfo

    ok = ImageInfo(
        status=200, content_type="image/png", fmt="png", width=256, height=256, size_bytes=4096
    )
    assert _run_icon(_icon_cat(), lambda url: ok) == []


def test_icon_image_flags_non_image():
    from vgi_lint_check.linkcheck import ImageInfo

    html = ImageInfo(status=200, content_type="text/html", fmt=None, size_bytes=1200)
    out = _run_icon(_icon_cat(), lambda url: html)
    assert out and out[0].code == "VGI015"
    assert "not a browser-displayable image" in out[0].message


def test_icon_image_flags_low_and_high_resolution():
    from vgi_lint_check.linkcheck import ImageInfo

    tiny = ImageInfo(status=200, fmt="png", width=16, height=16, size_bytes=200)
    out = _run_icon(_icon_cat(), lambda url: tiny)
    assert out and "min" in out[0].message

    huge = ImageInfo(status=200, fmt="png", width=4096, height=4096, size_bytes=5000)
    out = _run_icon(_icon_cat(), lambda url: huge)
    assert out and "max" in out[0].message


def test_icon_image_flags_oversized_bytes():
    from vgi_lint_check.linkcheck import ImageInfo

    heavy = ImageInfo(status=200, fmt="png", width=256, height=256, size_bytes=5_000_000)
    out = _run_icon(_icon_cat(), lambda url: heavy)
    assert out and "bytes" in out[0].message


def test_icon_image_flags_broken_status():
    from vgi_lint_check.linkcheck import ImageInfo

    out = _run_icon(_icon_cat(), lambda url: ImageInfo(status=404))
    assert out and "broken" in out[0].message


def test_icon_image_svg_has_no_resolution_check():
    from vgi_lint_check.linkcheck import ImageInfo

    svg = ImageInfo(status=200, content_type="image/svg+xml", fmt="svg", size_bytes=800)
    assert _run_icon(_icon_cat(), lambda url: svg) == []


def test_icon_image_silent_on_unreachable_and_no_probe():
    from vgi_lint_check.linkcheck import ImageInfo

    # network error -> stay quiet (not flaky)
    assert _run_icon(_icon_cat(), lambda url: ImageInfo(error="timed out")) == []
    # no probe wired (offline) -> no findings
    assert _run_icon(_icon_cat(), None) == []


def test_icon_image_gated_off_without_check_links():
    from vgi_lint_check.linkcheck import ImageInfo

    bad = ImageInfo(status=200, fmt=None)
    cat = _icon_cat()
    # requires_network rule is OFF unless check_links is set
    assert "VGI015" not in set(codes(cat, check_links=False))
    # sanity: the rule itself would fire when driven directly
    assert _run_icon(cat, lambda url: bad)
