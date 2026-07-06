"""Tests for the content rules: Markdown validity (VGI170) and link resolution (VGI171)."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.content import DescriptionLinksResolve


def codes(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


# --- VGI170 markdown validity (offline) -----------------------------------
def test_markdown_empty_link_target_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table with a broken markdown link in its description",
        tags={"vgi.doc_md": "See [the docs]() for details."},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI170" in set(codes(cat))


def test_markdown_unterminated_fence_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table whose markdown has an unterminated code fence here",
        tags={"vgi.doc_md": "Example:\n```sql\nSELECT 1"},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI170" in set(codes(cat))


def test_markdown_clean_passes():
    t = F.table(
        "main",
        "t",
        comment="A table with clean, valid markdown in its description text",
        tags={"vgi.doc_md": "See [docs](https://example.com).\n\n```sql\nSELECT 1\n```"},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI170" not in set(codes(cat))


# --- VGI171 link resolution (network rule, gated + resolver-injected) ------
def _run_links(cat, resolver, *, check_links=True):
    cfg = Config(check_links=check_links)
    ctx = RuleContext(cat, cfg, link_resolver=resolver)
    from vgi_lint_check.findings import Severity

    ctx.severity = Severity.WARNING
    return list(DescriptionLinksResolve().check(ctx))


def test_link_rule_flags_404_source_url():
    cat = F.catalog(F.schema("main"), source_url="https://example.com/missing")
    out = _run_links(cat, lambda url: 404)
    assert out and out[0].code == "VGI171"
    assert "source_url" in out[0].message


def test_link_rule_passes_on_2xx_and_skips_transient():
    cat = F.catalog(F.schema("main"), source_url="https://example.com/ok")
    assert _run_links(cat, lambda url: 200) == []  # reachable
    assert _run_links(cat, lambda url: None) == []  # unreachable -> skipped
    assert _run_links(cat, lambda url: 503) == []  # transient server error -> skipped


def test_link_rule_noop_without_resolver():
    cat = F.catalog(F.schema("main"), source_url="https://example.com/missing")
    # resolver None (offline) -> no network, no findings even if check_links on
    assert _run_links(cat, None) == []


def test_network_rule_off_by_default_without_check_links():
    # requires_network rules are gated OFF unless check_links is set
    cat = F.catalog(F.schema("main"), source_url="https://example.com/x")
    assert "VGI171" not in set(codes(cat, check_links=False))


def test_markdown_image_url_collected_for_resolution():
    t = F.table(
        "main",
        "t",
        comment="A table whose markdown embeds an image that 404s on resolution",
        tags={"vgi.doc_md": "![diagram](https://example.com/img.png)"},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    out = _run_links(cat, lambda url: 404)
    assert any("img.png" in f.message for f in out)


# --- VGI173 description-enumerates-objects ---------------------------------
def _calendar_catalog(catalog_doc):
    funcs = [F.func("main", n) for n in ("easter", "iso_week", "iso_year_week", "is_holiday")]
    tables = [F.table("main", n) for n in ("holidays", "business_days")]
    return F.catalog(
        F.schema("main", tables=tables, functions=funcs),
        tags={"vgi.doc_llm": catalog_doc, "vgi.doc_md": catalog_doc},
    )


def test_catalog_description_enumerating_objects_is_error():
    doc = (
        "This worker provides `easter`, `iso_week`, `iso_year_week`, and "
        "`is_holiday`, plus the `holidays` and `business_days` tables."
    )
    cat = _calendar_catalog(doc)
    out = [f for f in run(select_rules(Config()), RuleContext(cat, Config())) if f.code == "VGI173"]
    assert out and out[0].severity.name == "ERROR"


def test_purposeful_catalog_description_with_one_example_passes():
    # Names a single object in a call form — well under the floor/fraction.
    doc = (
        "Date-arithmetic helpers for analytics: resolve public holidays, ISO "
        "week boundaries, and business-day windows when joining time series. "
        "For example, `easter(2026)` returns Easter Sunday for a year."
    )
    cat = _calendar_catalog(doc)
    assert "VGI173" not in set(codes(cat))


def test_enumeration_ignores_english_word_names_in_prose():
    # 'holidays'/'business' appear as plain prose, not code tokens -> no match.
    doc = (
        "A calendar worker covering public holidays and business days across "
        "countries, with helpers for ISO week math and Easter computation, so "
        "analysts can bucket and align event time series without external data."
    )
    cat = _calendar_catalog(doc)
    assert "VGI173" not in set(codes(cat))


# --- VGI174 description-sql-fenced ------------------------------------------
def _sql_codes(doc_md):
    t = F.table("main", "t", comment="A table row of data here.", tags={"vgi.doc_md": doc_md})
    cat = F.catalog(F.schema("main", tables=[t]))
    return set(codes(cat))


def test_raw_sql_in_prose_flagged():
    assert "VGI174" in _sql_codes("Run SELECT date FROM cal.main.business_days to list days.")


def test_unlabeled_sql_fence_flagged():
    assert "VGI174" in _sql_codes("Example:\n\n```\nSELECT easter(2026)\n```\n")


def test_labeled_sql_fence_passes():
    assert "VGI174" not in _sql_codes("Example:\n\n```sql\nSELECT easter(2026)\n```\n")


def test_prose_without_sql_passes():
    assert "VGI174" not in _sql_codes("Select the right country before filtering by date.")


def test_inline_sql_span_flagged():
    # A runnable statement tucked into an inline `code` span (the calendar pattern).
    assert "VGI174" in _sql_codes("Try `SELECT cal.main.easter(2026)` to compute Easter.")


def test_bare_keyword_in_inline_span_passes():
    # `SELECT` as a keyword reference, not a statement -> not flagged.
    assert "VGI174" not in _sql_codes("Bring calendar math into DuckDB with just a `SELECT`.")


# --- VGI177 code-fence-declares-language -----------------------------------
def test_unlabeled_non_sql_fence_flagged():
    # A JSON snippet in a bare fence -> VGI177 (declare a language).
    codes_ = _sql_codes('Config:\n\n```\n{"tz": "UTC"}\n```\n')
    assert "VGI177" in codes_
    assert "VGI174" not in codes_  # not SQL, so VGI174 stays quiet


def test_labeled_fence_passes():
    assert "VGI177" not in _sql_codes('Config:\n\n```json\n{"tz": "UTC"}\n```\n')


def test_unlabeled_sql_fence_defers_to_vgi174():
    # An unlabeled SQL fence is VGI174's concern; VGI177 must not double-report it.
    codes_ = _sql_codes("Example:\n\n```\nSELECT easter(2026)\n```\n")
    assert "VGI174" in codes_
    assert "VGI177" not in codes_


def test_indented_block_not_flagged_by_vgi177():
    # Indented code blocks can't carry a language tag; VGI177 targets fences only.
    assert "VGI177" not in _sql_codes("Output:\n\n    some plain text\n")


# --- VGI175 listing-doc-uses-markdown / VGI176 multi-paragraph -------------
def test_vgi175_plain_prose_schema_doc_flagged():
    s = F.schema(
        "main", tags={"vgi.doc_md": "Just plain prose describing the schema, no markdown."}
    )
    assert "VGI175" in set(codes(F.catalog(s)))


def test_vgi175_structured_schema_doc_passes():
    s = F.schema("main", tags={"vgi.doc_md": "## Zoo\n\nAnimals and sounds.\n\n- a\n- b"})
    assert "VGI175" not in set(codes(F.catalog(s)))


def test_vgi176_single_paragraph_flagged():
    # a header + one prose paragraph counts as one paragraph (header excluded)
    s = F.schema("main", tags={"vgi.doc_md": "## Zoo\n\nOne single paragraph of prose here."})
    assert "VGI176" in set(codes(F.catalog(s)))


def test_vgi176_multi_paragraph_passes():
    s = F.schema(
        "main", tags={"vgi.doc_md": "First paragraph of the listing.\n\nSecond paragraph."}
    )
    assert "VGI176" not in set(codes(F.catalog(s)))
