"""End-to-end semantic contract tests over committed example workers."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import haybarn
import pytest
import yaml

from tests.fixtures import arg, col, func, table
from tests.fixtures import catalog as fixture_catalog
from tests.fixtures import schema as fixture_schema
from tests.semantic_example import load_example
from vgi_lint_check import simulate
from vgi_lint_check.config import Config
from vgi_lint_check.model import TAG_REQUIRED_FILTERS, ObjectId, ResultColumn
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
            "invocations": [],
            "effective_source_grain": [
                {
                    "source": "com.example.sales::orders",
                    "member": "order_id",
                    "output_name": "order_id",
                }
            ],
            "result_grain": ["country"],
            "estimated_invocations": 0,
            "driving_grain_reduced": False,
        }
    ]


def test_nested_struct_field_members_compile_and_satisfy_required_filters():
    places = table(
        "places",
        "place",
        tags={
            "vgi.semantic_entity": json.dumps({"entity_id": "place", "grain": ["place_id"]}),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "place_id", "kind": "identifier", "column": "id"},
                    {
                        "member_id": "category",
                        "kind": "dimension",
                        "column": "basic_category",
                    },
                    *[
                        {
                            "member_id": f"bbox_{axis}",
                            "kind": "dimension",
                            "column_path": ["bbox", axis],
                            "data_type": "DOUBLE",
                            "unit": "deg",
                            "hidden": True,
                        }
                        for axis in ("xmin", "xmax", "ymin", "ymax")
                    ],
                    {
                        "member_id": "place_count",
                        "kind": "measure",
                        "aggregation": "count_rows",
                        "additivity": "additive",
                    },
                ]
            ),
            TAG_REQUIRED_FILTERS: json.dumps(
                [["bbox.xmin"], ["bbox.xmax"], ["bbox.ymin"], ["bbox.ymax"]]
            ),
        },
        columns=[
            col("places", "place", "id"),
            col("places", "place", "basic_category"),
            col(
                "places",
                "place",
                "bbox",
                dtype="STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE)",
            ),
        ],
    )
    worker = fixture_catalog(
        fixture_schema("places", tables=[places]),
        tags={
            "vgi.semantic_catalog": json.dumps(
                {"catalog_id": "farm.query.maps", "binding_key": "maps"}
            )
        },
    )
    predicates = [
        {
            "member": f"bbox_{axis}",
            "operator": operator,
            "value": value,
        }
        for axis, operator, value in (
            ("xmin", "gte", 4.88),
            ("xmax", "lte", 4.91),
            ("ymin", "gte", 52.36),
            ("ymax", "lte", 52.38),
        )
    ]
    result = compile_semantic_query(
        {"v": worker},
        {
            "measures": [
                {
                    "catalog_id": "farm.query.maps",
                    "entity_id": "place",
                    "member_id": "place_count",
                }
            ],
            "dimensions": [
                {
                    "catalog_id": "farm.query.maps",
                    "entity_id": "place",
                    "member_id": "category",
                }
            ],
            "filters": {"and": predicates},
        },
    )
    assert result["ok"] is True, result
    assert '_e0."bbox"."xmin" >= ?' in result["plan"]["sql"]
    assert '_e0."bbox"."ymax" <= ?' in result["plan"]["sql"]
    assert result["plan"]["parameters"] == [4.88, 4.91, 52.36, 52.38]


def test_literal_dotted_column_name_is_not_treated_as_a_struct_path():
    source = table(
        "source",
        "events",
        tags={
            "vgi.semantic_entity": json.dumps({"entity_id": "events", "grain": ["id"]}),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "id", "kind": "identifier", "column": "id"},
                    {
                        "member_id": "literal_dot",
                        "kind": "dimension",
                        "column": "literal.dot",
                    },
                    {
                        "member_id": "event_count",
                        "kind": "measure",
                        "aggregation": "count_rows",
                        "additivity": "additive",
                    },
                ]
            ),
            TAG_REQUIRED_FILTERS: json.dumps([["literal.dot"]]),
        },
        columns=[col("source", "events", "id"), col("source", "events", "literal.dot")],
    )
    worker = fixture_catalog(
        fixture_schema("source", tables=[source]),
        tags={
            "vgi.semantic_catalog": json.dumps(
                {"catalog_id": "com.example.dotted", "binding_key": "dotted"}
            )
        },
    )
    result = compile_semantic_query(
        {"worker": worker},
        {
            "measures": [
                {
                    "catalog_id": "com.example.dotted",
                    "entity_id": "events",
                    "member_id": "event_count",
                }
            ],
            "filters": {"member": "literal_dot", "operator": "eq", "value": "yes"},
        },
    )
    assert result["ok"] is True, result
    assert '_e0."literal.dot" = ?' in result["plan"]["sql"]
    assert '_e0."literal"."dot"' not in result["plan"]["sql"]


@pytest.mark.parametrize(
    ("predicate", "root_member", "root_type", "expected"),
    [
        (
            {
                "from_member": "geometry",
                "to_member": "geometry",
                "operator": "spatial_within",
            },
            {"member_id": "geometry", "kind": "dimension", "column": "geometry", "hidden": True},
            "GEOMETRY",
            'ST_Within(_e0."geometry", _e1."geometry")',
        ),
        (
            {
                "from_member": "connectors",
                "to_member": "zone_id",
                "operator": "list_contains",
                "from_element_path": ["connector_id"],
            },
            {
                "member_id": "connectors",
                "kind": "dimension",
                "column": "connectors",
                "hidden": True,
            },
            "STRUCT(connector_id VARCHAR)[]",
            'list_contains(list_transform(_e0."connectors", '
            '_item -> _item."connector_id"), _e1."id")',
        ),
    ],
)
def test_typed_relationship_predicates_compile_without_raw_sql(
    predicate, root_member, root_type, expected
):
    relationship = {
        "relationship_id": "com.example.event_zone",
        "from": {"catalog_id": "com.example.geo", "entity_id": "events"},
        "to": {"catalog_id": "com.example.geo", "entity_id": "zones"},
        "from_cardinality": {"min": 0, "max": "many"},
        "to_cardinality": {"min": 0, "max": 1},
        "predicate": [predicate],
        "conditions": [{"side": "to", "member": "zone_type", "value": "district"}],
    }
    events = table(
        "geo",
        "events",
        tags={
            "vgi.semantic_entity": json.dumps({"entity_id": "events", "grain": ["event_id"]}),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "event_id", "kind": "identifier", "column": "id"},
                    root_member,
                    {
                        "member_id": "event_count",
                        "kind": "measure",
                        "aggregation": "count_rows",
                        "additivity": "additive",
                    },
                ]
            ),
            "vgi.semantic_relationships": json.dumps([relationship]),
        },
        columns=[
            col("geo", "events", "id"),
            col("geo", "events", root_member["column"], dtype=root_type),
        ],
    )
    zones = table(
        "geo",
        "zones",
        tags={
            "vgi.semantic_entity": json.dumps({"entity_id": "zones", "grain": ["zone_id"]}),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "zone_id", "kind": "identifier", "column": "id"},
                    {"member_id": "zone_name", "kind": "dimension", "column": "name"},
                    {"member_id": "zone_type", "kind": "dimension", "column": "type"},
                    {
                        "member_id": "geometry",
                        "kind": "dimension",
                        "column": "geometry",
                        "hidden": True,
                    },
                ]
            ),
        },
        columns=[
            col("geo", "zones", "id"),
            col("geo", "zones", "name"),
            col("geo", "zones", "type"),
            col("geo", "zones", "geometry", dtype="GEOMETRY"),
        ],
    )
    worker = fixture_catalog(
        fixture_schema("geo", tables=[events, zones]),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "com.example.geo"})},
    )
    result = compile_semantic_query(
        {"geo": worker},
        {
            "measures": [
                {
                    "catalog_id": "com.example.geo",
                    "entity_id": "events",
                    "member_id": "event_count",
                }
            ],
            "dimensions": [
                {
                    "catalog_id": "com.example.geo",
                    "entity_id": "zones",
                    "member_id": "zone_name",
                }
            ],
        },
    )
    assert result["ok"] is True, result
    assert expected in result["plan"]["sql"]
    assert '_e1."type" IS NOT DISTINCT FROM ?' in result["plan"]["sql"]
    assert result["plan"]["parameters"] == ["district"]


def test_fixed_schema_table_macro_compiles_as_a_scalar_single_fact_source():
    historical = func(
        "gers",
        "changelog_at",
        ftype="table_macro",
        parameters=["release"],
        arguments=[arg("release", position=0, field_index=0, is_positional=True)],
        tags={
            "vgi.result_columns_schema": json.dumps(
                [
                    {"name": "id", "type": "VARCHAR", "description": "GERS identifier"},
                    {
                        "name": "change_type",
                        "type": "VARCHAR",
                        "description": "Change classification",
                    },
                ]
            ),
            "vgi.semantic_entity": json.dumps(
                {
                    "entity_id": "historical_changelog",
                    "grain": ["id"],
                    "source": {
                        "arguments": [
                            {"argument": "release", "parameter": "release", "required": True}
                        ]
                    },
                }
            ),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "id", "kind": "identifier", "column": "id"},
                    {
                        "member_id": "change_type",
                        "kind": "dimension",
                        "column": "change_type",
                    },
                    {
                        "member_id": "record_count",
                        "kind": "measure",
                        "aggregation": "count_rows",
                        "additivity": "additive",
                    },
                ]
            ),
        },
    )
    historical.id = ObjectId("v", historical.kind, schema="gers", name="changelog_at")
    worker = fixture_catalog(
        fixture_schema("gers", functions=[historical]),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "farm.query.maps"})},
    )
    result = compile_semantic_query(
        {"maps": worker},
        {
            "measures": [
                {
                    "catalog_id": "farm.query.maps",
                    "entity_id": "historical_changelog",
                    "member_id": "record_count",
                }
            ],
            "parameters": {"release": "2026-06-17.0"},
        },
    )
    assert result["ok"] is True, result
    assert 'FROM "v"."gers"."changelog_at"(?) AS _e0' in result["plan"]["sql"]
    assert result["plan"]["parameters"] == ["2026-06-17.0"]


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
        assert 'FROM "v"."main"."events"(?, "region" := ?) AS _e0' in compiled["plan"]["sql"]
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


def _forecast_catalog():
    function = func(
        "main",
        "forecast_hourly",
        "table",
        tags={
            "vgi.semantic_entity": json.dumps(
                {
                    "entity_id": "forecast_hourly",
                    "grain": ["time_key"],
                    "source": {
                        "arguments": [
                            {"argument": "latitude", "parameter": "latitude"},
                            {"argument": "longitude", "parameter": "longitude"},
                            {
                                "argument": "forecast_days",
                                "parameter": "forecast_days",
                                "required": False,
                            },
                        ]
                    },
                }
            ),
            "vgi.semantic_members": json.dumps(
                [
                    {"member_id": "time_key", "kind": "identifier", "column": "time"},
                    {
                        "member_id": "time",
                        "kind": "time_dimension",
                        "column": "time",
                        "timezone": "UTC",
                        "granularities": ["hour", "day"],
                    },
                    {
                        "member_id": "temperature",
                        "kind": "dimension",
                        "column": "temperature",
                        "data_type": "DOUBLE",
                    },
                    {
                        "member_id": "average_temperature",
                        "kind": "measure",
                        "aggregation": "avg",
                        "member": "temperature",
                        "additivity": "non_additive",
                    },
                ]
            ),
        },
        arguments=[
            arg("latitude", "DOUBLE", position=0, field_index=0, is_positional=True),
            arg("longitude", "DOUBLE", position=1, field_index=1, is_positional=True),
            arg("forecast_days", "INTEGER", field_index=2, is_named=True, default="3"),
        ],
        input_from_args=True,
        result_columns=[
            ResultColumn("time", "TIMESTAMP", "Forecast hour."),
            ResultColumn("temperature", "DOUBLE", "Temperature."),
        ],
    )
    return fixture_catalog(
        fixture_schema("main", functions=[function]),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "farm.query.open_meteo"})},
    )


def test_inline_input_compiles_and_executes_correlated_lateral_function():
    worker = _rehome(_forecast_catalog(), "weather")
    function = next(worker.iter_all_functions())
    function.tags.raw[TAG_REQUIRED_FILTERS] = json.dumps([["latitude"]])
    connection = haybarn.connect()
    try:
        connection.execute("ATTACH ':memory:' AS weather")
        connection.execute(
            "CREATE MACRO weather.main.forecast_hourly"
            "(latitude, longitude, forecast_days := 3) AS TABLE "
            "SELECT TIMESTAMP '2026-01-01' + i * INTERVAL 1 HOUR AS time, "
            "latitude + longitude + i AS temperature FROM range(forecast_days) r(i)"
        )
        request = {
            "measures": [
                {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                    "member_id": "average_temperature",
                }
            ],
            "dimensions": [
                {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                    "member_id": "time",
                    "granularity": "hour",
                }
            ],
            "inputs": [
                {
                    "input_id": "locations",
                    "grain": ["location_id"],
                    "columns": [
                        {"name": "location_id", "type": "VARCHAR"},
                        {"name": "latitude", "type": "DOUBLE"},
                        {"name": "longitude", "type": "DOUBLE"},
                    ],
                    "rows": [["berlin", 52.52, 13.41], ["tokyo", 35.69, 139.69]],
                }
            ],
            "source_bindings": [
                {
                    "entity": {
                        "catalog_id": "farm.query.open_meteo",
                        "entity_id": "forecast_hourly",
                    },
                    "driver": {"input_id": "locations"},
                    "arguments": {
                        "latitude": {"input_column": "latitude"},
                        "longitude": {"input_column": "longitude"},
                        "forecast_days": {"parameter": "forecast_days"},
                    },
                }
            ],
            "parameters": {"forecast_days": 2},
            "order": [
                {"member": "location_id", "direction": "asc"},
                {"member": "time", "direction": "asc"},
            ],
        }
        compiled = compile_semantic_query({"weather": worker}, request)
        assert compiled["ok"] is True, compiled
        assert "CROSS JOIN LATERAL" in compiled["plan"]["sql"]
        assert (
            '"weather"."main"."forecast_hourly"('
            '"_driver"."latitude", "_driver"."longitude", "forecast_days" := ?)'
            in compiled["plan"]["sql"]
        )
        assert compiled["plan"]["parameters"] == ["berlin", 52.52, 13.41, "tokyo", 35.69, 139.69, 2]
        branch = compiled["plan"]["fact_branches"][0]
        assert branch["estimated_invocations"] == 2
        assert branch["result_grain"] == ["location_id", "time"]
        reduced = compile_semantic_query(
            {"weather": worker},
            {
                **request,
                "order": [{"member": "time", "direction": "asc"}],
                "allow_driving_grain_reduction": True,
            },
        )
        assert reduced["ok"] is True
        assert reduced["plan"]["fact_branches"][0]["driving_grain_reduced"] is True
        assert reduced["plan"]["fact_branches"][0]["result_grain"] == ["time"]

        function.input_from_args = False
        unsupported = compile_semantic_query({"weather": worker}, request)
        assert unsupported["ok"] is False
        assert unsupported["diagnostics"][0]["code"] == "correlated_input_not_supported"
        function.input_from_args = None
        unknown = compile_semantic_query({"weather": worker}, request)
        assert unknown["ok"] is False
        assert unknown["diagnostics"][0]["code"] == "correlated_input_capability_unknown"
        assert "upgrade" in unknown["diagnostics"][0]["message"]
        function.input_from_args = True

        bad_type = deepcopy(request)
        bad_type["inputs"][0]["rows"][0][1] = "not-a-latitude"
        rejected_type = compile_semantic_query({"weather": worker}, bad_type)
        assert rejected_type["ok"] is False
        assert rejected_type["diagnostics"][0]["code"] == "incompatible_input_value"

        too_many_calls = {**request, "execution_limits": {"max_invocations": 1}}
        rejected_limit = compile_semantic_query({"weather": worker}, too_many_calls)
        assert rejected_limit["ok"] is False
        assert rejected_limit["diagnostics"][0]["code"] == "invocation_limit"
        result = execute_semantic_query({"weather": worker}, connection, request)
        assert result["ok"] is True, result
        assert len(result["result"]["rows"]) == 4
    finally:
        connection.close()


def test_units_are_reported_for_static_defaulted_and_explicit_outputs():
    worker = _rehome(_forecast_catalog(), "weather")
    function = next(worker.iter_all_functions())
    entity = json.loads(function.tags.raw["vgi.semantic_entity"])
    entity["source"]["arguments"].append(
        {
            "argument": "temperature_unit",
            "parameter": "temperature_unit",
            "required": False,
        }
    )
    function.tags.raw["vgi.semantic_entity"] = json.dumps(entity)
    members = json.loads(function.tags.raw["vgi.semantic_members"])
    next(item for item in members if item["member_id"] == "temperature")["unit_parameter"] = {
        "argument": "temperature_unit",
        "values": {"celsius": "Cel", "fahrenheit": "[degF]"},
    }
    next(item for item in members if item["member_id"] == "time")["unit"] = "s"
    function.tags.raw["vgi.semantic_members"] = json.dumps(members)
    function.arguments.append(
        arg(
            "temperature_unit",
            "VARCHAR",
            field_index=3,
            is_named=True,
            default='"celsius"',
            choices='["celsius", "fahrenheit"]',
        )
    )
    request = {
        "measures": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "average_temperature",
            }
        ],
        "dimensions": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "time",
            }
        ],
        "parameters": {"latitude": 52.52, "longitude": 13.41},
    }
    defaulted = compile_semantic_query({"weather": worker}, request)
    assert defaulted["ok"] is True, defaulted
    assert validate_instance("result", defaulted) == []
    assert defaulted["plan"]["output_units"] == {
        "time": "s",
        "average_temperature": "Cel",
    }
    explicit = compile_semantic_query(
        {"weather": worker},
        {**request, "parameters": {**request["parameters"], "temperature_unit": "fahrenheit"}},
    )
    assert explicit["ok"] is True, explicit
    assert explicit["plan"]["output_units"]["average_temperature"] == "[degF]"
    assert explicit["plan"]["parameters"] == [52.52, 13.41, "fahrenheit"]

    bad = compile_semantic_query(
        {"weather": worker},
        {**request, "parameters": {**request["parameters"], "temperature_unit": "kelvin"}},
    )
    assert bad["ok"] is False
    assert bad["diagnostics"][0]["code"] == "unit_parameter_value_unmapped"


def test_correlated_unit_value_is_reported_as_unresolved_metadata():
    worker = _rehome(_forecast_catalog(), "weather")
    function = next(worker.iter_all_functions())
    entity = json.loads(function.tags.raw["vgi.semantic_entity"])
    entity["source"]["arguments"].append(
        {"argument": "temperature_unit", "parameter": "temperature_unit"}
    )
    function.tags.raw["vgi.semantic_entity"] = json.dumps(entity)
    members = json.loads(function.tags.raw["vgi.semantic_members"])
    next(item for item in members if item["member_id"] == "temperature")["unit_parameter"] = {
        "argument": "temperature_unit",
        "values": {"celsius": "Cel", "fahrenheit": "[degF]"},
    }
    function.tags.raw["vgi.semantic_members"] = json.dumps(members)
    function.arguments.append(
        arg(
            "temperature_unit",
            "VARCHAR",
            position=2,
            field_index=3,
            is_positional=True,
            default='"celsius"',
        )
    )
    request = {
        "measures": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "average_temperature",
            }
        ],
        "inputs": [
            {
                "input_id": "locations",
                "grain": ["location_id"],
                "columns": [
                    {"name": "location_id", "type": "VARCHAR"},
                    {"name": "latitude", "type": "DOUBLE"},
                    {"name": "longitude", "type": "DOUBLE"},
                    {"name": "temperature_unit", "type": "VARCHAR"},
                ],
                "rows": [["berlin", 52.52, 13.41, "fahrenheit"]],
            }
        ],
        "source_bindings": [
            {
                "entity": {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                },
                "driver": {"input_id": "locations"},
                "arguments": {
                    "latitude": {"input_column": "latitude"},
                    "longitude": {"input_column": "longitude"},
                    "temperature_unit": {"input_column": "temperature_unit"},
                },
            }
        ],
    }
    result = compile_semantic_query({"weather": worker}, request)
    assert result["ok"] is True, result
    assert result["plan"]["output_units"] == {"average_temperature": None}
    assert result["plan"]["unit_diagnostics"][0]["code"] == ("unit_parameter_value_unresolved")
    assert "fahrenheit" in result["plan"]["parameters"]
    assert validate_instance("result", result) == []


def test_static_measure_unit_is_reported_without_changing_sql_or_parameters():
    worker = _rehome(_forecast_catalog(), "weather")
    function = next(worker.iter_all_functions())
    members = json.loads(function.tags.raw["vgi.semantic_members"])
    next(item for item in members if item["member_id"] == "average_temperature")["unit"] = "percent"
    function.tags.raw["vgi.semantic_members"] = json.dumps(members)
    request = {
        "measures": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "average_temperature",
            }
        ],
        "parameters": {"latitude": 52.52, "longitude": 13.41},
    }
    baseline = compile_semantic_query({"weather": _rehome(_forecast_catalog(), "weather")}, request)
    result = compile_semantic_query({"weather": worker}, request)
    assert result["ok"] is True, result
    assert result["plan"]["output_units"] == {"average_temperature": "percent"}
    assert result["plan"]["sql"] == baseline["plan"]["sql"]
    assert result["plan"]["parameters"] == baseline["plan"]["parameters"]


def test_scalar_function_remains_usable_when_correlated_capability_is_unknown():
    worker = _rehome(_forecast_catalog(), "weather")
    next(worker.iter_all_functions()).input_from_args = None
    result = compile_semantic_query(
        {"weather": worker},
        {
            "measures": [
                {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                    "member_id": "average_temperature",
                }
            ],
            "parameters": {"latitude": 52.52, "longitude": 13.41},
        },
    )
    assert result["ok"] is True, result


def _sites_catalog():
    sites = fixture_catalog(
        fixture_schema(
            "main",
            tables=[
                table(
                    "main",
                    "sites",
                    tags={
                        "vgi.semantic_entity": json.dumps(
                            {"entity_id": "sites", "grain": ["site_id"]}
                        ),
                        "vgi.semantic_members": json.dumps(
                            [
                                {
                                    "member_id": "site_id",
                                    "kind": "identifier",
                                    "column": "site_id",
                                    "data_type": "VARCHAR",
                                },
                                {
                                    "member_id": "latitude",
                                    "kind": "dimension",
                                    "column": "latitude",
                                    "data_type": "DOUBLE",
                                },
                                {
                                    "member_id": "longitude",
                                    "kind": "dimension",
                                    "column": "longitude",
                                    "data_type": "DOUBLE",
                                },
                                {
                                    "member_id": "site_name",
                                    "kind": "dimension",
                                    "column": "site_name",
                                    "data_type": "VARCHAR",
                                },
                            ]
                        ),
                    },
                    columns=[
                        col("main", "sites", "site_id", dtype="VARCHAR"),
                        col("main", "sites", "latitude", dtype="DOUBLE"),
                        col("main", "sites", "longitude", dtype="DOUBLE"),
                        col("main", "sites", "site_name", dtype="VARCHAR"),
                    ],
                )
            ],
        ),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "com.example.assets"})},
    )
    return sites


def test_entity_driven_lateral_execution_preserves_driver_grain():
    sites = _rehome(_sites_catalog(), "assets")
    next(sites.iter_tables()).tags.raw[TAG_REQUIRED_FILTERS] = json.dumps([["site_id"]])
    weather = _rehome(_forecast_catalog(), "weather")
    connection = haybarn.connect()
    try:
        connection.execute("ATTACH ':memory:' AS assets")
        connection.execute("ATTACH ':memory:' AS weather")
        connection.execute(
            "CREATE TABLE assets.main.sites"
            "(site_id VARCHAR, latitude DOUBLE, longitude DOUBLE, site_name VARCHAR)"
        )
        connection.execute(
            "INSERT INTO assets.main.sites VALUES "
            "('berlin', 52.52, 13.41, 'Berlin'), "
            "('tokyo', 35.69, 139.69, 'Tokyo')"
        )
        connection.execute(
            "CREATE MACRO weather.main.forecast_hourly"
            "(latitude, longitude, forecast_days := 3) AS TABLE "
            "SELECT TIMESTAMP '2026-01-01' AS time, latitude + longitude AS temperature"
        )
        request = {
            "measures": [
                {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                    "member_id": "average_temperature",
                }
            ],
            "dimensions": [
                {
                    "catalog_id": "com.example.assets",
                    "entity_id": "sites",
                    "member_id": "site_name",
                }
            ],
            "source_bindings": [
                {
                    "entity": {
                        "catalog_id": "farm.query.open_meteo",
                        "entity_id": "forecast_hourly",
                    },
                    "driver": {
                        "entity": {"catalog_id": "com.example.assets", "entity_id": "sites"},
                        "max_rows": 2,
                        "filters": {"member": "site_id", "operator": "eq", "value": "berlin"},
                        "order": [{"member_id": "site_id", "direction": "asc"}],
                    },
                    "arguments": {
                        "latitude": {
                            "member": {
                                "catalog_id": "com.example.assets",
                                "entity_id": "sites",
                                "member_id": "latitude",
                            }
                        },
                        "longitude": {
                            "member": {
                                "catalog_id": "com.example.assets",
                                "entity_id": "sites",
                                "member_id": "longitude",
                            }
                        },
                    },
                }
            ],
            "parameters": {"forecast_days": 1},
        }
        result = execute_semantic_query({"assets": sites, "weather": weather}, connection, request)
        assert result["ok"] is True, result
        assert result["result"]["columns"] == [
            "site_id",
            "site_name",
            "average_temperature",
        ]
        assert len(result["result"]["rows"]) == 1
        missing = deepcopy(request)
        del missing["source_bindings"][0]["driver"]["filters"]
        rejected = compile_semantic_query({"assets": sites, "weather": weather}, missing)
        assert rejected["ok"] is False
        assert rejected["diagnostics"][0]["code"] == "driver_required_filter_missing"
    finally:
        connection.close()


def _geocode_catalog():
    function = func(
        "main",
        "geocode",
        "table",
        input_from_args=True,
        arguments=[
            arg("latitude", "DOUBLE", position=0, field_index=0, is_positional=True),
            arg("longitude", "DOUBLE", position=1, field_index=1, is_positional=True),
        ],
        result_columns=[
            ResultColumn("candidate_id", "VARCHAR", "Candidate."),
            ResultColumn("latitude", "DOUBLE", "Latitude."),
            ResultColumn("longitude", "DOUBLE", "Longitude."),
        ],
        tags={
            "vgi.semantic_entity": json.dumps(
                {
                    "entity_id": "geocode",
                    "grain": ["candidate_id"],
                    "source": {
                        "arguments": [
                            {"argument": "latitude", "parameter": "latitude"},
                            {"argument": "longitude", "parameter": "longitude"},
                        ]
                    },
                }
            ),
            "vgi.semantic_members": json.dumps(
                [
                    {
                        "member_id": "candidate_id",
                        "kind": "identifier",
                        "column": "candidate_id",
                        "data_type": "VARCHAR",
                    },
                    {
                        "member_id": "latitude",
                        "kind": "dimension",
                        "column": "latitude",
                        "data_type": "DOUBLE",
                    },
                    {
                        "member_id": "longitude",
                        "kind": "dimension",
                        "column": "longitude",
                        "data_type": "DOUBLE",
                    },
                ]
            ),
        },
    )
    return fixture_catalog(
        fixture_schema("main", functions=[function]),
        tags={"vgi.semantic_catalog": json.dumps({"catalog_id": "com.example.geocoding"})},
    )


def test_chained_correlated_functions_compile_as_bounded_acyclic_pipeline():
    geocode = _rehome(_geocode_catalog(), "geo")
    weather = _rehome(_forecast_catalog(), "weather")
    request = {
        "measures": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "average_temperature",
            }
        ],
        "inputs": [
            {
                "input_id": "locations",
                "grain": ["location_id"],
                "columns": [
                    {"name": "location_id", "type": "VARCHAR"},
                    {"name": "latitude", "type": "DOUBLE"},
                    {"name": "longitude", "type": "DOUBLE"},
                ],
                "rows": [["berlin", 52.52, 13.41], ["tokyo", 35.69, 139.69]],
            }
        ],
        "source_bindings": [
            {
                "entity": {"catalog_id": "farm.query.open_meteo", "entity_id": "forecast_hourly"},
                "driver": {
                    "entity": {"catalog_id": "com.example.geocoding", "entity_id": "geocode"},
                    "max_rows": 4,
                },
                "arguments": {
                    "latitude": {
                        "member": {
                            "catalog_id": "com.example.geocoding",
                            "entity_id": "geocode",
                            "member_id": "latitude",
                        }
                    },
                    "longitude": {
                        "member": {
                            "catalog_id": "com.example.geocoding",
                            "entity_id": "geocode",
                            "member_id": "longitude",
                        }
                    },
                },
            },
            {
                "entity": {"catalog_id": "com.example.geocoding", "entity_id": "geocode"},
                "driver": {"input_id": "locations"},
                "arguments": {
                    "latitude": {"input_column": "latitude"},
                    "longitude": {"input_column": "longitude"},
                },
            },
        ],
        "parameters": {"forecast_days": 1},
        "execution_limits": {"max_invocations": 6},
    }
    compiled = compile_semantic_query({"geo": geocode, "weather": weather}, request)
    assert compiled["ok"] is True, compiled
    branch = compiled["plan"]["fact_branches"][0]
    assert [item["entity"]["entity_id"] for item in branch["invocations"]] == [
        "geocode",
        "forecast_hourly",
    ]
    assert branch["estimated_invocations"] == 6
    assert branch["result_grain"] == ["location_id", "candidate_id"]
    assert compiled["plan"]["sql"].count("CROSS JOIN LATERAL") == 2

    connection = haybarn.connect()
    try:
        connection.execute("ATTACH ':memory:' AS geo")
        connection.execute("ATTACH ':memory:' AS weather")
        connection.execute(
            "CREATE MACRO geo.main.geocode(latitude, longitude) AS TABLE "
            "SELECT 'best' AS candidate_id, latitude, longitude"
        )
        connection.execute(
            "CREATE MACRO weather.main.forecast_hourly"
            "(latitude, longitude, forecast_days := 3) AS TABLE "
            "SELECT TIMESTAMP '2026-01-01' AS time, latitude + longitude AS temperature"
        )
        executed = execute_semantic_query({"geo": geocode, "weather": weather}, connection, request)
        assert executed["ok"] is True, executed
        assert executed["result"]["columns"] == [
            "location_id",
            "candidate_id",
            "average_temperature",
        ]
        assert len(executed["result"]["rows"]) == 2
    finally:
        connection.close()

    cyclic = deepcopy(request)
    cyclic["source_bindings"][1]["driver"] = {
        "entity": {"catalog_id": "farm.query.open_meteo", "entity_id": "forecast_hourly"},
        "max_rows": 2,
    }
    rejected = compile_semantic_query({"geo": geocode, "weather": weather}, cyclic)
    assert rejected["ok"] is False
    assert rejected["diagnostics"][0]["code"] == "correlation_cycle"


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
        for function in schema.functions:
            function.id = ObjectId(
                alias, function.id.kind, schema=function.schema, name=function.name
            )
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
