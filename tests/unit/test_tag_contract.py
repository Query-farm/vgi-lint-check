import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from vgi_lint_check.cli import app
from vgi_lint_check.config import from_table, load_config
from vgi_lint_check.model import TAG_DOC_LLM, TAG_RESULT_COLUMNS_SCHEMA, TagSet
from vgi_lint_check.tag_spec import TagContractError, contract, validate_contract
from vgi_lint_check.tags import decode_agent_test_tasks, merge_agent_task_sidecar


def test_spec_command_exports_constants():
    result = CliRunner().invoke(app, ["spec", "--format", "json"])
    assert result.exit_code == 0
    spec = json.loads(result.output)
    values = {item["symbol"]: item["key"] for item in spec["tags"]}
    assert spec["contract_revision"] == 1
    assert values["TAG_DOC_LLM"] == TAG_DOC_LLM
    assert values["TAG_RESULT_COLUMNS_SCHEMA"] == TAG_RESULT_COLUMNS_SCHEMA


def test_bundled_contract_is_valid():
    validate_contract()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda spec: spec["tags"].append(deepcopy(spec["tags"][0])),
            "duplicate symbol",
        ),
        (
            lambda spec: spec["aliases"][0].update(canonical="vgi.not_a_tag"),
            "references unknown active key",
        ),
        (
            lambda spec: spec["retired"][0].update(replacement=["vgi.not_a_tag"]),
            "references unknown active keys",
        ),
        (
            lambda spec: spec.update(contract_revision=999),
            "unsupported contract_revision",
        ),
    ],
)
def test_contract_validation_rejects_inconsistent_contracts(mutate, message):
    spec = deepcopy(contract())
    mutate(spec)
    with pytest.raises(TagContractError, match=message):
        validate_contract(spec)


def test_every_lint_run_validates_contract_before_loading_worker(monkeypatch):
    def invalid_contract():
        raise TagContractError("duplicate key 'vgi.doc_llm'")

    monkeypatch.setattr("vgi_lint_check.cli.validate_contract", invalid_contract)
    result = CliRunner().invoke(app, ["lint", "unused-worker"])
    assert result.exit_code != 0
    assert "invalid bundled tag contract: duplicate key 'vgi.doc_llm'" in result.output


def test_public_tasks_ignore_embedded_graders_and_sidecar_merges_them(tmp_path):
    tasks, error = decode_agent_test_tasks(
        TagSet({
            "vgi.agent_test_tasks": json.dumps([{
                "name": "lookup",
                "prompt": "Find the row",
                "check_sql": "SELECT leaked",
            }]),
        })
    )
    assert error is None
    assert tasks[0].check_sql is None

    sidecar = tmp_path / "vgi-agent-tests.yaml"
    sidecar.write_text(
        "tasks:\n"
        "  - name: lookup\n"
        "    success_criteria: Returns one row\n"
        "    reference_sql: SELECT 1 AS value\n"
        "    check_sql: SELECT count(*) = 1\n",
        encoding="utf-8",
    )
    merged = merge_agent_task_sidecar(tasks, sidecar)
    assert merged[0].success_criteria == "Returns one row"
    assert merged[0].reference_statements[0].sql == "SELECT 1 AS value"
    assert merged[0].check_sql == "SELECT count(*) = 1"


def test_agent_task_sidecar_is_configurable():
    config = from_table({"simulate": {"agent_tasks_file": "private/tasks.yaml"}})
    assert config.agent_tasks_file == "private/tasks.yaml"


def test_conventional_agent_task_sidecar_is_discovered(tmp_path):
    sidecar = tmp_path / "vgi-agent-tests.yaml"
    sidecar.write_text("tasks: []\n", encoding="utf-8")
    assert load_config(start_dir=tmp_path).agent_tasks_file == str(sidecar.resolve())
