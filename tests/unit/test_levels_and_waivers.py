"""Assurance levels, structured waivers, the waiver audit, and the fleet sweep."""

import json
import textwrap

import pytest

from vgi_lint_check import levels
from vgi_lint_check.config import Waiver, from_table, load_config
from vgi_lint_check.findings import Category, Finding, Severity
from vgi_lint_check.levels import Level
from vgi_lint_check.model import ObjectId, ObjectKind
from vgi_lint_check.rules.base import Rule, RuleContext


def _finding(code, severity=Severity.WARNING):
    return Finding(
        code=code,
        severity=severity,
        category=Category.STRUCTURE,
        object_id=ObjectId(database="c", kind=ObjectKind.CATALOG),
        message="m",
        hint="h",
    )


# --- levels ---------------------------------------------------------------
def test_tier_of_derives_from_rule_gating_flags():
    # VGI112 is plain static; VGI901 needs a connection; VGI180 needs the judge.
    assert levels.tier_of("VGI112") == "structural"
    assert levels.tier_of("VGI901") == "behavioral"
    assert levels.tier_of("VGI180") == "semantic"
    # An unknown code (older/newer linter) must not crash the ladder.
    assert levels.tier_of("VGI999999") == "structural"


def test_clean_static_lint_stops_at_l1_and_says_why():
    rep = levels.compute([], executed=False, doc_reviewed=False, agent_checked=False)
    assert rep.level is Level.L1
    assert "no-execute" in rep.blocker


def test_execute_reaches_l2_and_names_the_missing_llm_passes():
    rep = levels.compute([], executed=True, doc_reviewed=False, agent_checked=False)
    assert rep.level is Level.L2
    assert "--doc-review" in rep.blocker and "--agent-check" in rep.blocker


def test_all_passes_reach_l3_and_tutorials_unlock_l4():
    rep = levels.compute([], executed=True, doc_reviewed=True, agent_checked=True)
    assert rep.level is Level.L3
    assert rep.blocker == "tutorials not assessed"
    rep4 = levels.compute([], executed=True, doc_reviewed=True, agent_checked=True, tutorials=True)
    assert rep4.level is Level.L4


def test_a_structural_warning_drops_to_l0_even_when_everything_ran():
    rep = levels.compute([_finding("VGI112")], executed=True, doc_reviewed=True, agent_checked=True)
    assert rep.level is Level.L0
    assert "structural" in rep.blocker


def test_info_findings_never_hold_a_level_back():
    rep = levels.compute(
        [_finding("VGI112", Severity.INFO)], executed=True, doc_reviewed=False, agent_checked=False
    )
    assert rep.level is Level.L2


def test_execution_failure_caps_at_l1_not_l2():
    rep = levels.compute(
        [_finding("VGI901", Severity.ERROR)],
        executed=True,
        doc_reviewed=True,
        agent_checked=True,
    )
    assert rep.level is Level.L1


# --- structured waivers ---------------------------------------------------
def test_bare_string_ignore_still_works_and_lands_in_unspecified():
    cfg = from_table({"ignore": ["VGI146"]})
    assert cfg.ignore == ["VGI146"]
    assert cfg.waivers == [Waiver(code="VGI146")]
    assert "no reason/kind recorded" in " ".join(cfg.waivers[0].problems())


def test_table_form_captures_reason_and_kind():
    cfg = from_table(
        {
            "ignore": [
                {"code": "VGI146", "reason": "passthrough connector", "kind": "domain-exemption"}
            ]
        }
    )
    assert cfg.ignore == ["VGI146"]
    w = cfg.waivers[0]
    assert w.kind == "domain-exemption" and w.reason == "passthrough connector"
    assert w.problems() == []


def test_per_object_waivers_record_their_scope():
    cfg = from_table(
        {
            "per_object": {
                "cat.main.t": {
                    "ignore": [{"code": "VGI807", "reason": "keyless stream", "kind": "deferred"}]
                }
            }
        }
    )
    assert cfg.per_object == {"cat.main.t": ["VGI807"]}
    assert cfg.waivers[0].scope == "cat.main.t"
    assert cfg.waivers[0].where == "cat.main.t"


@pytest.mark.parametrize(
    "raw,needle",
    [
        ({"code": "V", "kind": "nonsense", "reason": "r"}, "unknown kind"),
        ({"code": "V", "kind": "deferred"}, "without a reason"),
        ({"code": "V", "kind": "deferred", "reason": "r", "expires": "nope"}, "not an ISO date"),
        ({"code": "V", "kind": "deferred", "reason": "r", "expires": "2001-01-01"}, "expired on"),
    ],
)
def test_waiver_problems_are_reported(raw, needle):
    cfg = from_table({"ignore": [raw]})
    assert any(needle in p for p in cfg.waivers[0].problems())


def test_mixed_string_and_table_entries_coexist():
    cfg = from_table({"ignore": ["VGI111", {"code": "VGI112", "kind": "timing", "reason": "r"}]})
    assert cfg.ignore == ["VGI111", "VGI112"]
    assert [w.kind for w in cfg.waivers] == ["unspecified", "timing"]


def test_is_waived_distinguishes_a_waiver_from_every_other_reason_a_rule_is_off():
    from vgi_lint_check.rules.registry import REGISTRY

    static_rule = REGISTRY["VGI112"]
    exec_rule = REGISTRY["VGI901"]

    cfg = from_table({"ignore": ["VGI112"]})
    assert cfg.is_waived(static_rule) is True
    # VGI901 is off because --no-execute, not because of a waiver: the audit must
    # not resurrect it (that would run SQL during a static lint).
    cfg.execute = False
    assert cfg.is_waived(exec_rule) is False


def test_config_file_round_trips_structured_waivers(tmp_path):
    (tmp_path / "vgi-lint.toml").write_text(
        textwrap.dedent(
            """
            [tool.vgi-lint-check]
            ignore = [
              { code = "VGI146", reason = "argument-only worker", kind = "domain-exemption" },
            ]
            """
        )
    )
    cfg = load_config(start_dir=tmp_path)
    assert cfg.ignore == ["VGI146"]
    assert cfg.waivers[0].kind == "domain-exemption"


# --- the audit ------------------------------------------------------------
class _Quiet(Rule):
    code = "VGI112"
    name = "quiet"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING

    def check(self, ctx):
        return iter(())


class _Noisy(Rule):
    code = "VGI111"
    name = "noisy"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING

    def check(self, ctx):
        yield _finding("VGI111")


def test_audit_marks_a_waiver_dead_only_when_its_rule_really_finds_nothing(monkeypatch):
    from vgi_lint_check.rules import engine, registry

    monkeypatch.setattr(registry, "all_rule_classes", lambda: [_Quiet, _Noisy])
    cfg = from_table({"ignore": ["VGI112", "VGI111"]})
    ctx = RuleContext(catalog=None, config=cfg)  # these rules never touch the catalog
    usage = {u.waiver.code: u for u in engine.audit_waivers([], ctx)}
    assert usage["VGI112"].dead is True
    assert usage["VGI111"].dead is False and usage["VGI111"].suppressed == 1


def test_audit_sources_waived_rules_from_the_registry_not_the_caller_list(monkeypatch):
    """A catalog-wide waiver's rule is absent from select_rules() output.

    select_rules() drops every OFF rule, so the waived ones never appear in the
    list the pipeline hands to the engine. Auditing that list would find nothing
    to run and declare every catalog-wide waiver dead — advice that would delete
    load-bearing suppressions. Regression test for exactly that.
    """
    from vgi_lint_check.rules import engine, registry

    monkeypatch.setattr(registry, "all_rule_classes", lambda: [_Noisy])
    cfg = from_table({"ignore": ["VGI111"]})
    ctx = RuleContext(catalog=None, config=cfg)
    # Caller list is EMPTY, exactly as select_rules() would return it.
    (usage,) = engine.audit_waivers([], ctx)
    assert usage.dead is False and usage.suppressed == 1


def test_report_fails_on_a_dead_waiver_only_when_the_audit_ran():
    from vgi_lint_check.config import WaiverUsage
    from vgi_lint_check.result import Report, VersionResult

    dead = WaiverUsage(waiver=Waiver(code="VGI146", kind="domain-exemption", reason="r"))
    vr = VersionResult(catalog=None, findings=[], quality=None, waiver_audit=[dead])
    unaudited = Report("l", "a", None, [vr], Severity.ERROR)
    assert unaudited.passed() is True  # nothing was audited; nothing to claim

    audited = Report("l", "a", None, [vr], Severity.ERROR, audited_waivers=True)
    assert audited.passed() is False
    assert audited.dead_waivers() == [dead]


# --- fleet ----------------------------------------------------------------
def test_fleet_manifest_round_trip(tmp_path):
    from vgi_lint_check import fleet

    (tmp_path / "m.toml").write_text(
        textwrap.dedent(
            """
            [defaults]
            execute = false

            [[worker]]
            name = "a"
            location = "./a"
            directory = "."

            [[worker]]
            name = "b"
            location = "./b"
            execute = true
            tags = ["flagship"]
            """
        )
    )
    specs = fleet.load_manifest(tmp_path / "m.toml")
    assert [s.name for s in specs] == ["a", "b"]
    assert specs[0].execute is False  # inherited the default
    assert specs[1].execute is True  # per-worker override wins
    assert specs[1].tags == ["flagship"]


def test_fleet_command_reflects_the_spec():
    from vgi_lint_check import fleet

    spec = fleet.WorkerSpec(name="w", location="./w", execute=False, agent_check=True)
    cmd = fleet._build_command(spec, linter=["vgi-lint"])
    assert "--no-execute" in cmd and "--agent-check" in cmd
    assert "--audit-waivers" in cmd
    # The sweep must never gate a single worker — it aggregates and gates once.
    assert cmd[cmd.index("--fail-on") + 1] == "never"


def test_fleet_distills_a_lint_document():
    from vgi_lint_check import fleet

    doc = {
        "worker": {"vgi_version": "abc"},
        "results": [
            {
                "score": 91,
                "static_score": 95,
                "level": {"level": 2, "label": "L2", "title": "behavioral", "blocker": "b"},
                "counts": {"error": 1, "warning": 2, "info": 0},
                "waivers": [
                    {"code": "VGI1", "kind": "tooling-bug", "dead": False},
                    {"code": "VGI2", "kind": "domain-exemption", "dead": True},
                ],
                "findings": [
                    {"code": "A", "severity": "info", "object": {}, "message": "m"},
                    {"code": "B", "severity": "error", "object": {}, "message": "m"},
                ],
            }
        ],
    }
    res = fleet._distill(fleet.WorkerResult(name="w", status="skipped"), doc)
    assert res.status == "ok" and res.score == 91 and res.level == 2
    assert res.dead_waivers == 1
    assert [b["code"] for b in res.tooling_bugs] == ["VGI1"]
    # errors rank above info in the distilled top findings
    assert res.top_findings[0]["severity"] == "error"


def test_fleet_summary_and_renderers():
    from vgi_lint_check import fleet

    results = [
        fleet.WorkerResult(
            name="a",
            status="ok",
            score=100,
            level=3,
            level_label="L3",
            counts={"error": 0, "warning": 0, "info": 0},
        ),
        fleet.WorkerResult(
            name="b",
            status="ok",
            score=80,
            level=1,
            level_label="L1",
            counts={"error": 2, "warning": 1, "info": 0},
            dead_waivers=1,
            waivers=[{"code": "X", "kind": "deferred", "dead": True}],
        ),
        fleet.WorkerResult(name="c", status="timeout"),
    ]
    s = fleet.summarize(results)
    assert s["workers"] == 3 and s["linted"] == 2 and s["timeout"] == 1
    assert s["score_mean"] == 90.0 and s["score_min"] == 80
    assert s["by_level"] == {"L3": 1, "L1": 1}
    assert s["dead_waivers"] == 1
    assert s["findings"]["error"] == 2

    doc = fleet.to_document(results, linter_version="9.9.9")
    assert json.loads(json.dumps(doc))["linter_version"] == "9.9.9"
    md = fleet.render_markdown(doc)
    assert "| a | L3 | 100" in md
    html = fleet.render_html(doc)
    # Self-contained and not accidentally broken by str.format on the CSS.
    assert html.startswith("<!doctype html>") and "{" not in html.split("<style>")[0]
    assert "9.9.9" in html and "timeout" in html


def test_fleet_parses_json_after_leading_noise():
    from vgi_lint_check import fleet

    assert fleet._parse_json('warning: something\n{"a": 1}') == {"a": 1}
    assert fleet._parse_json("") is None
    assert fleet._parse_json("not json at all") is None


# --- credential refusals are not scan failures ----------------------------
@pytest.mark.parametrize(
    "message",
    [
        "VGI Worker Exception: Error: audit_logs: attach an 'azure_graph' secret"
        " (TYPE azure_graph)",
        "no AWS credentials found for the kinesis stream",
        "Error: an api_key is required for this provider",
        "authentication required: create a secret first",
        "CREATE SECRET (TYPE llm, ...) before querying",
        "401 Unauthorized",
        "the ldap secret is not configured",
    ],
)
def test_credential_refusals_are_recognized(message):
    from vgi_lint_check.rules._util import is_credential_error

    assert is_credential_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        'Binder Error: referenced column "foo" not found',
        "Catalog Error: Table with name x does not exist",
        "query exceeded 30s and was cancelled",
        "connection reset by peer",
        # An auth-adjacent word alone must not excuse a real failure — the
        # classifier is deliberately narrow.
        "the authenticated user has no rows in this table",
    ],
)
def test_real_failures_are_not_mistaken_for_credential_refusals(message):
    from vgi_lint_check.rules._util import is_credential_error

    assert is_credential_error(Exception(message)) is False


# --- the sweep must lint what CI lints -----------------------------------
def test_ci_location_accepts_an_interpreter_plus_script(tmp_path):
    """A CI location is often a command, not a bare binary.

    Several Python workers lint `.venv/bin/python worker.py` in CI while a naive
    guess would pick `uv run worker.py` — which resolves a *different* SDK
    environment, so the sweep reports failures CI never sees. Regression test for
    that: only the leading token has to exist.
    """
    from vgi_lint_check import fleet

    repo = tmp_path / "vgi-thing"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (repo / ".venv" / "bin" / "python").chmod(0o755)
    (repo / "thing_worker.py").write_text("# worker\n")
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  lint:\n    steps:\n      - uses: Query-farm/vgi-lint-check@v1\n"
        '        with:\n          location: ".venv/bin/python thing_worker.py"\n'
    )
    loc = fleet._location_from_ci(repo)
    assert loc.endswith("thing_worker.py")
    assert ".venv/bin/python" in loc
    # ...and it wins over the `uv run *_worker.py` fallback.
    assert fleet._infer_location(repo) == loc


def test_ci_location_skipped_when_its_artifact_is_absent(tmp_path):
    from vgi_lint_check import fleet

    repo = tmp_path / "vgi-thing"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  lint:\n    steps:\n      - uses: Query-farm/vgi-lint-check@v1\n"
        '        with:\n          location: "target/release/thing-worker"\n'
    )
    assert fleet._location_from_ci(repo) == ""


def test_ci_location_skips_unresolvable_templates(tmp_path):
    from vgi_lint_check import fleet

    repo = tmp_path / "vgi-thing"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  lint:\n    steps:\n      - uses: Query-farm/vgi-lint-check@v1\n"
        "        with:\n          location: ${{ env.VGI_TIKA_WORKER }}\n"
    )
    assert fleet._location_from_ci(repo) == ""


def test_jvm_worker_located_by_its_shaded_jar(tmp_path):
    """JVM workers template their CI location through an env var.

    `location: "java -jar ${{ env.VGI_TIKA_WORKER }}"` cannot be resolved from
    the workflow, so those repos were skipped entirely by the sweep even though
    the built artifact was sitting in build/libs. Fall back to the newest fat jar.
    """
    from vgi_lint_check import fleet

    repo = tmp_path / "vgi-thing"
    (repo / "build" / "libs").mkdir(parents=True)
    (repo / "build" / "libs" / "vgi-thing-0.1.0-all.jar").write_bytes(b"x")
    (repo / "build" / "libs" / "vgi-thing-0.1.0.jar").write_bytes(b"x")  # not shaded
    loc = fleet._infer_location(repo)
    assert loc.startswith("java -jar ")
    assert loc.endswith("-all.jar")


# --- concurrency artifacts must not be reported as worker defects ---------
def test_timing_sensitive_findings_are_detected():
    from vgi_lint_check import fleet

    r = fleet.WorkerResult(
        name="w",
        status="ok",
        top_findings=[
            {"code": "VGI911", "severity": "error"},
            {"code": "VGI515", "severity": "error"},  # deterministic — not suspect
            {"code": "VGI908", "severity": "warning"},
        ],
    )
    assert fleet.timing_sensitive_findings(r) == ["VGI908", "VGI911"]
    clean = fleet.WorkerResult(name="w", status="ok", top_findings=[{"code": "VGI515"}])
    assert fleet.timing_sensitive_findings(clean) == []


def test_sweep_remeasures_timing_findings_serially(monkeypatch):
    """A scan starved by a concurrent sweep is not an unresponsive scan.

    Heavy workers (scipy, a JVM, a JIT) can starve their neighbours past the scan
    timeout, so VGI911/VGI908 seen under concurrency may belong to the sweep, not
    the worker. Those get re-measured with the machine to itself and the serial
    verdict wins — otherwise the sweep sends people to fix problems they do not
    have. (Observed for real on vgi-survival: L1 in a 4-way sweep, L2 alone.)
    """
    from vgi_lint_check import fleet

    calls = []

    def fake_lint_one(spec, linter=None):
        calls.append(spec.name)
        if len(calls) == 1:  # concurrent pass: spurious timeout
            return fleet.WorkerResult(
                name=spec.name,
                status="ok",
                score=94,
                level=1,
                level_label="L1",
                top_findings=[{"code": "VGI911", "severity": "error"}],
            )
        return fleet.WorkerResult(  # serial pass: clean
            name=spec.name, status="ok", score=100, level=2, level_label="L2"
        )

    monkeypatch.setattr(fleet, "lint_one", fake_lint_one)
    spec = fleet.WorkerSpec(name="vgi-heavy", location="./w")
    (out,) = fleet.sweep([spec], jobs=4)
    assert out.level == 2 and out.score == 100
    assert out.reverified == ["VGI911"]
    assert len(calls) == 2


def test_sweep_keeps_a_finding_that_survives_the_serial_rerun(monkeypatch):
    from vgi_lint_check import fleet

    def fake_lint_one(spec, linter=None):
        return fleet.WorkerResult(
            name=spec.name,
            status="ok",
            score=86,
            level=1,
            level_label="L1",
            top_findings=[{"code": "VGI911", "severity": "error"}],
        )

    monkeypatch.setattr(fleet, "lint_one", fake_lint_one)
    (out,) = fleet.sweep([fleet.WorkerSpec(name="w", location="./w")], jobs=4)
    assert out.level == 1  # a genuinely unresponsive scan fails both times


def test_sweep_does_not_reverify_when_serial(monkeypatch):
    from vgi_lint_check import fleet

    calls = []

    def fake_lint_one(spec, linter=None):
        calls.append(spec.name)
        return fleet.WorkerResult(name=spec.name, status="ok", top_findings=[{"code": "VGI911"}])

    monkeypatch.setattr(fleet, "lint_one", fake_lint_one)
    fleet.sweep([fleet.WorkerSpec(name="w", location="./w")], jobs=1)
    assert len(calls) == 1


def test_execution_waivers_are_unconfirmed_not_dead(monkeypatch):
    """A quiet execution rule is not evidence that its waiver is dead.

    VGI901/VGI902 verdicts move with session state and ordering — a catalog-level
    executable example can create a model that a later per-function example then
    binds against, so the same waiver reads live in one run and dead in the next.
    Observed on vgi-lightgbm: VGI901 "dead" on predict but live on explain, with
    an identical rationale. Advising deletion on one observation is how a
    load-bearing waiver gets deleted.
    """
    from vgi_lint_check.rules import engine, registry

    monkeypatch.setattr(registry, "all_rule_classes", lambda: [_Quiet])
    cfg = from_table({"ignore": ["VGI901", "VGI112"]})
    cfg.execute = True
    ctx = RuleContext(catalog=None, config=cfg)
    usage = {u.waiver.code: u for u in engine.audit_waivers([], ctx)}

    # Execution rule: quiet, but not condemned.
    assert usage["VGI901"].unconfirmed is True
    assert usage["VGI901"].dead is False
    # Static rule: a quiet pass is conclusive.
    assert usage["VGI112"].unconfirmed is False
    assert usage["VGI112"].dead is True


# --- the agent run inherits the execution window --------------------------
def test_agent_check_inherits_the_execution_window():
    """--agent-check drives the same worker through the same cold start.

    A worker that legitimately needs a wide window (a 2GB model load, a slow
    upstream) declares it once under [execution]. Before this, `simulate` used its
    own hardcoded 30s, so such a worker failed the agent check for a reason that
    had nothing to do with agent usability — blocking L3 for exactly the workers
    careful enough to configure themselves correctly.
    """
    cfg = from_table({"execution": {"timeout": 120, "concurrency": 1}})
    limits = cfg.sim_limits()
    assert limits.timeout == 120
    assert limits.concurrency == 1


def test_simulate_section_overrides_the_inherited_window():
    cfg = from_table(
        {
            "execution": {"timeout": 120, "concurrency": 1},
            "simulate": {"timeout": 300, "concurrency": 2, "attempts": 2, "max_steps": 20},
        }
    )
    limits = cfg.sim_limits()
    assert limits.timeout == 300
    assert limits.concurrency == 2
    assert limits.attempts == 2
    assert limits.max_steps == 20


def test_sim_limits_fall_back_to_defaults_with_no_config():
    cfg = from_table({})
    limits = cfg.sim_limits()
    assert limits.timeout == cfg.execute_timeout
    assert limits.concurrency == cfg.execute_concurrency


# --- the standalone `simulate` command honours config too -----------------
def test_simulate_cli_inherits_config_and_lets_flags_win(tmp_path, monkeypatch):
    """`vgi-lint simulate` must resolve limits the way `--agent-check` does.

    The fix that made --agent-check inherit the [execution] window initially
    missed this path, so `simulate --verify-references` still used a hardcoded
    30s and no config could reach it — the exact blocker it was meant to clear.
    This drives the real CLI, which is also what catches that SimLimits is frozen
    (the first attempt mutated it and crashed at runtime).
    """
    from click.testing import CliRunner

    from vgi_lint_check import cli, core

    (tmp_path / "vgi-lint.toml").write_text(
        "[tool.vgi-lint-check.execution]\ntimeout = 120\nconcurrency = 1\n"
    )
    monkeypatch.chdir(tmp_path)

    captured = {}

    def fake_attach(location, runner, **kw):
        # `runner` closes over `limits`; grab them from its cell contents.
        for cell in runner.__closure__ or ():
            val = cell.cell_contents
            if type(val).__name__ == "SimLimits":
                captured["limits"] = val
        raise SystemExit(0)

    monkeypatch.setattr(core, "with_attached_catalog", fake_attach)

    CliRunner().invoke(cli.app, ["simulate", "./w", "--verify-references"])
    lim = captured["limits"]
    assert lim.timeout == 120  # inherited from [execution], not the old 30s
    assert lim.concurrency == 1

    captured.clear()
    CliRunner().invoke(
        cli.app, ["simulate", "./w", "--verify-references", "--query-timeout", "300"]
    )
    assert captured["limits"].timeout == 300  # an explicit flag still wins
