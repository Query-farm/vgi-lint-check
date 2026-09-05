"""End-to-end semantic contract tests over committed example workers."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import haybarn
import pytest
import yaml

from tests.fixtures import arg, func
from tests.fixtures import catalog as fixture_catalog
from tests.fixtures import schema as fixture_schema
from tests.semantic_example import load_example
from vgi_lint_check import simulate
from vgi_lint_check.config import Config
from vgi_lint_check.model import ObjectId, ResultColumn
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.semantic_compiler import compile_semantic_query, execute_semantic_query
from vgi_lint_check.semantic_federation import build_federated_semantic_model
from vgi_lint_check.semantic_schema import validate_instance
from vgi_lint_check.tag_spec import validate_contract
from vgi_lint_check.tags import merge_agent_task_sidecar


@pytest.fixture
def commerce():
    fixture, catalogs, connection = load_example()
    try:
        yield fixture, catalogs, connection
    finally:
        connection.close()


def _cells(rows):
    return [
        [str(value) if isinstance(value, Decimal) else str(value) for value in row] for row in rows
    ]


def test_example_workers_load_lint_and_federate_without_semantic_errors(commerce):
    _fixture, catalogs, _connection = commerce
    validate_contract()
    config = Config(select=["VGI418", "VGI419", "VGI420", "VGI421"])
    for catalog in catalogs.values():
        assert run(select_rules(config), RuleContext(catalog, config)) == []

    graph = build_federated_semantic_model(catalogs)
    assert graph.diagnostics == []
    relationship = graph.relationships["com.example.sales.order_customer"]
    assert relationship.resolution_status == "resolved"
    assert relationship.attestation == "corroborated"


def test_every_committed_request_compiles_executes_and_matches_expected_rows(commerce):
    fixture, catalogs, connection = commerce
    for case in fixture["queries"]:
        compiled = compile_semantic_query(catalogs, case["request"])
        assert compiled["ok"] is True, (case["name"], compiled)
        assert validate_instance("result", compiled) == []
        executed = execute_semantic_query(catalogs, connection.cursor(), case["request"])
        assert executed["ok"] is True, (case["name"], executed)
        assert executed["result"]["columns"] == case["expected_columns"]
        assert _cells(executed["result"]["rows"]) == case["expected_rows"]


def test_cross_catalog_plan_is_deterministic_and_parameterized(commerce):
    _fixture, catalogs, _connection = commerce
    request = {
        "measures": [
            {
                "catalog_id": "com.example.sales",
                "entity_id": "orders",
                "member_id": "revenue",
            }
        ],
        "dimensions": [
            {
                "catalog_id": "com.example.crm",
                "entity_id": "customers",
                "member_id": "country",
            }
        ],
        "filters": {
            "member": {
                "catalog_id": "com.example.crm",
                "entity_id": "customers",
                "member_id": "segment",
            },
            "operator": "eq",
            "value": "growth",
        },
        "order": [{"member": "revenue", "direction": "desc"}],
    }
    first = compile_semantic_query(catalogs, request)
    second = compile_semantic_query(catalogs, request)
    assert first == second
    assert first["plan"]["parameters"] == ["growth"]
    sql = first["plan"]["sql"]
    assert 'FROM "sales_runtime"."main"."orders" AS _e0' in sql
    assert 'INNER JOIN "crm_runtime"."main"."customers" AS _e1' in sql
    assert 'WHERE _e1."segment" = ?' in sql
    assert first["plan"]["fact_branches"] == [
        {
            "root": {"catalog_id": "com.example.sales", "entity_id": "orders"},
            "attachment_alias": "sales_runtime",
            "entities": [
                "sales_runtime:com.example.sales::orders",
                "crm_runtime:com.example.crm::customers",
            ],
        }
    ]


def _table_function_catalog(*, hole: bool = False):
    physical_arguments = (
        [
            arg("first", position=0, field_index=0, is_positional=True, default="0"),
            arg("last", position=1, field_index=1, is_positional=True),
        ]
        if hole
        else [
            arg("since", position=0, field_index=0, is_positional=True),
            arg("region", field_index=1, is_named=True, default='"all"'),
        ]
    )
    mappings = (
        [
            {"argument": "first", "parameter": "first", "required": False},
            {"argument": "last", "parameter": "last"},
        ]
        if hole
        else [
            {"argument": "region", "parameter": "region", "required": False},
            {"argument": "since", "parameter": "start"},
        ]
    )
    function = func(
        "main",
        "events",
        "table",
        tags={
            "vgi.semantic_entity": json.dumps(
                {
                    "entity_id": "events",
                    "grain": ["event_id"],
                    "source": {"arguments": mappings},
                }
            ),
            "vgi.semantic_members": json.dumps(
                [
                    {
                        "member_id": "event_id",
                        "kind": "identifier",
                        "column": "event_id",
                    },
                    {
                        "member_id": "region",
                        "kind": "dimension",
                        "column": "source_region",
                    },
                    {
                        "member_id": "amount",
                        "kind": "dimension",
                        "column": "amount",
                    },
                    {
                        "member_id": "revenue",
                        "kind": "measure",
                        "aggregation": "sum",
                        "member": "amount",
                        "additivity": "additive",
                    },
                ]
            ),
        },
        arguments=physical_arguments,
        result_columns=[
            ResultColumn("event_id", "INTEGER", "Event identifier."),
            ResultColumn("source_region", "VARCHAR", "Event region."),
            ResultColumn("amount", "INTEGER", "Event amount."),
        ],
    )
    return fixture_catalog(
        fixture_schema("main", functions=[function]),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "com.example.events"})},
    )


def test_table_function_compiler_executes_positional_then_named_arguments():
    worker = _table_function_catalog()
    connection = haybarn.connect()
    try:
        connection.execute("ATTACH ':memory:' AS v")
        connection.execute(
            "CREATE TABLE v.main.event_rows "
            "(event_id INTEGER, happened_at TIMESTAMP, source_region VARCHAR, amount INTEGER)"
        )
        connection.execute(
            "INSERT INTO v.main.event_rows VALUES "
            "(1, '2026-01-01', 'US', 10), (2, '2026-02-01', 'CA', 20), "
            "(3, '2026-03-01', 'US', 30)"
        )
        connection.execute(
            "CREATE MACRO v.main.events(since, region := 'all') AS TABLE "
            "SELECT event_id, source_region, amount FROM v.main.event_rows "
            "WHERE happened_at >= since AND (region = 'all' OR source_region = region)"
        )
        request = {
            "measures": [
                {
                    "catalog_id": "com.example.events",
                    "entity_id": "events",
                    "member_id": "revenue",
                }
            ],
            "dimensions": [
                {
                    "catalog_id": "com.example.events",
                    "entity_id": "events",
                    "member_id": "region",
                }
            ],
            "parameters": {"start": "2026-01-15", "region": "US"},
        }
        compiled = compile_semantic_query({"v": worker}, request)
        assert compiled["ok"] is True
        assert 'FROM "v"."main"."events"(?, "region" := ?) AS _e0' in compiled["plan"][
            "sql"
        ]
        assert compiled["plan"]["parameters"] == ["2026-01-15", "US"]
        defaulted = compile_semantic_query(
            {"v": worker},
            {
                **request,
                "parameters": {"start": "2026-01-15"},
            },
        )
        assert defaulted["ok"] is True
        assert 'FROM "v"."main"."events"(?) AS _e0' in defaulted["plan"]["sql"]
        assert defaulted["plan"]["parameters"] == ["2026-01-15"]
        executed = execute_semantic_query({"v": worker}, connection, request)
        assert executed["ok"] is True
        assert executed["result"]["rows"] == [("US", 30)]
    finally:
        connection.close()


def test_table_function_compiler_rejects_optional_positional_holes():
    result = compile_semantic_query(
        {"v": _table_function_catalog(hole=True)},
        {
            "root_entity": {"catalog_id": "com.example.events", "entity_id": "events"},
            "dimensions": [
                {
                    "catalog_id": "com.example.events",
                    "entity_id": "events",
                    "member_id": "event_id",
                }
            ],
            "parameters": {"last": 10},
        },
    )
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == "optional_positional_hole"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ([], "missing_function_argument_metadata"),
        (
            [
                arg("since", position=0, field_index=0, is_positional=True),
                arg("since", position=0, field_index=0, is_positional=True),
                arg("region", field_index=1, is_named=True, default='"all"'),
            ],
            "ambiguous_function_overload",
        ),
        (
            [
                arg("since", field_index=0, is_varargs=True),
                arg("region", field_index=1, is_named=True, default='"all"'),
            ],
            "unsupported_function_varargs",
        ),
    ],
)
def test_table_function_compiler_rejects_unsupported_signatures(arguments, code):
    worker = _table_function_catalog()
    function = next(worker.iter_all_functions())
    function.arguments = arguments
    result = compile_semantic_query(
        {"v": worker},
        {
            "root_entity": {"catalog_id": "com.example.events", "entity_id": "events"},
            "dimensions": [
                {
                    "catalog_id": "com.example.events",
                    "entity_id": "events",
                    "member_id": "event_id",
                }
            ],
            "parameters": {"start": 0},
        },
    )
    assert result["ok"] is False
    assert code in {diagnostic["code"] for diagnostic in result["diagnostics"]}


def test_compile_only_never_consults_the_connection(commerce):
    fixture, catalogs, _connection = commerce

    class NoDatabase:
        def execute(self, _sql, _parameters=None):
            raise AssertionError("compile_only must not touch DuckDB")

    request = {**fixture["queries"][0]["request"], "compile_only": True}
    result = execute_semantic_query(catalogs, NoDatabase(), request)
    assert result["ok"] is True
    assert "result" not in result


def test_compiler_rejects_fanout_and_multiple_fact_roots(commerce):
    _fixture, catalogs, _connection = commerce
    fanout = compile_semantic_query(
        catalogs,
        {
            "root_entity": {"catalog_id": "com.example.crm", "entity_id": "customers"},
            "dimensions": [
                {
                    "catalog_id": "com.example.crm",
                    "entity_id": "customers",
                    "member_id": "country",
                },
                {
                    "catalog_id": "com.example.sales",
                    "entity_id": "orders",
                    "member_id": "order_id",
                },
            ],
        },
    )
    assert fanout["ok"] is False
    assert fanout["diagnostics"][0]["stage"] == "fanout"

    augmented = deepcopy(catalogs)
    crm = augmented["crm_runtime"]
    customers = next(crm.iter_tables())
    members = json.loads(customers.tags.raw["vgi.semantic_members"])
    members.append(
        {
            "member_id": "customer_count",
            "kind": "measure",
            "aggregation": "count_rows",
            "additivity": "additive",
            "description": "Number of customers.",
        }
    )
    customers.tags.raw["vgi.semantic_members"] = json.dumps(members)
    multi_fact = compile_semantic_query(
        augmented,
        {
            "measures": [
                {
                    "catalog_id": "com.example.sales",
                    "entity_id": "orders",
                    "member_id": "revenue",
                },
                {
                    "catalog_id": "com.example.crm",
                    "entity_id": "customers",
                    "member_id": "customer_count",
                },
            ]
        },
    )
    assert multi_fact["ok"] is False
    assert multi_fact["diagnostics"][0]["stage"] == "multi_fact_not_supported"


def _rehome(catalog, alias):
    catalog.database = alias
    for schema in catalog.schemas:
        schema.database = alias
        schema.id = ObjectId(alias, schema.id.kind, schema=schema.name)
        for relation in [*schema.tables, *schema.views]:
            relation.id = ObjectId(
                alias, relation.id.kind, schema=relation.schema, name=relation.name
            )
            relation.columns = [
                replace(
                    column,
                    id=ObjectId(
                        alias,
                        column.id.kind,
                        schema=relation.schema,
                        name=relation.name,
                        column=column.name,
                    ),
                )
                for column in relation.columns
            ]
    return catalog


def test_duplicate_logical_catalog_requires_a_runtime_binding(commerce):
    _fixture, catalogs, _connection = commerce
    duplicated = {
        **catalogs,
        "crm_backup": _rehome(deepcopy(catalogs["crm_runtime"]), "crm_backup"),
    }
    request = {
        "root_entity": {"catalog_id": "com.example.crm", "entity_id": "customers"},
        "dimensions": [
            {
                "catalog_id": "com.example.crm",
                "entity_id": "customers",
                "member_id": "country",
            }
        ],
    }
    ambiguous = compile_semantic_query(duplicated, request)
    assert ambiguous["ok"] is False
    assert ambiguous["diagnostics"][0]["code"] == "ambiguous_catalog_binding"
    bound = compile_semantic_query(
        duplicated, {**request, "bindings": {"crm": "crm_backup"}, "compile_only": True}
    )
    assert bound["ok"] is True
    assert 'FROM "crm_backup"' in bound["plan"]["sql"]


class _Backend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_agent_discovers_and_uses_semantic_tool_with_execution_grading(commerce, tmp_path):
    fixture, catalogs, connection = commerce
    sidecar = tmp_path / "semantic-agent-tests.yaml"
    sidecar.write_text(yaml.safe_dump(fixture["agent_graders"]), encoding="utf-8")
    public_tasks = [task for catalog in catalogs.values() for task in catalog.agent_test_tasks]
    tasks = merge_agent_task_sidecar(public_tasks, sidecar)
    request = fixture["queries"][0]["request"]
    backend = _Backend(
        [
            json.dumps({"thought": "find stable catalog IDs", "action": "list_catalogs"}),
            json.dumps(
                {
                    "thought": "inspect the sales model",
                    "action": "describe_table",
                    "catalog": "sales_runtime",
                    "schema": "main",
                    "table": "orders",
                }
            ),
            json.dumps(
                {
                    "thought": "inspect the related customer dimensions",
                    "action": "describe_table",
                    "catalog": "crm_runtime",
                    "schema": "main",
                    "table": "customers",
                }
            ),
            json.dumps(
                {"thought": "use the declared model", "action": "query_semantic_model", **request}
            ),
            json.dumps(
                {
                    "thought": "the semantic result answers the task",
                    "action": "final",
                    "answer_summary": "CA has 200.00 revenue and US has 175.00.",
                }
            ),
        ]
    )
    report = simulate.simulate_tasks(
        list(catalogs.values()),
        connection,
        backend,
        limits=simulate.SimLimits(concurrency=1),
        tasks=tasks,
    )
    assert report.pass_rate == 1.0
    assert report.verdicts[0].grader == "reference"
    assert report.verdicts[0].queries == 1
    assert "query_semantic_model" in "\n".join(backend.prompts)


def test_semantic_tool_requirement_is_a_real_grading_gate(commerce, tmp_path):
    fixture, catalogs, connection = commerce
    sidecar = tmp_path / "semantic-agent-tests.yaml"
    sidecar.write_text(yaml.safe_dump(fixture["agent_graders"]), encoding="utf-8")
    public_tasks = [task for catalog in catalogs.values() for task in catalog.agent_test_tasks]
    tasks = merge_agent_task_sidecar(public_tasks, sidecar)
    backend = _Backend(
        [
            json.dumps(
                {
                    "action": "query_semantic_model",
                    "measures": [
                        {
                            "catalog_id": "com.example.sales",
                            "entity_id": "orders",
                            "member_id": "not_a_measure",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "action": "final",
                    "answer_sql": fixture["agent_graders"]["tasks"][0]["reference_sql"],
                }
            ),
        ]
    )
    report = simulate.simulate_tasks(
        list(catalogs.values()),
        connection,
        backend,
        limits=simulate.SimLimits(concurrency=1),
        tasks=tasks,
    )
    assert report.pass_rate == 0
    assert report.verdicts[0].grader == "required_tools"
    assert "query_semantic_model" in report.verdicts[0].reason
