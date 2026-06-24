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
        tags={"vgi.description_md": "See [the docs]() for details."},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI170" in set(codes(cat))


def test_markdown_unterminated_fence_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table whose markdown has an unterminated code fence here",
        tags={"vgi.description_md": "Example:\n```sql\nSELECT 1"},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI170" in set(codes(cat))


def test_markdown_clean_passes():
    t = F.table(
        "main",
        "t",
        comment="A table with clean, valid markdown in its description text",
        tags={"vgi.description_md": "See [docs](https://example.com).\n\n```sql\nSELECT 1\n```"},
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
        tags={"vgi.description_md": "![diagram](https://example.com/img.png)"},
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    out = _run_links(cat, lambda url: 404)
    assert any("img.png" in f.message for f in out)
