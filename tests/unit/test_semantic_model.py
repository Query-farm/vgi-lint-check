import json
from copy import deepcopy
from dataclasses import replace

from tests.fixtures import arg, catalog, col, func, schema, table
from vgi_lint_check.model import ObjectId
from vgi_lint_check.semantic_federation import build_federated_semantic_model
from vgi_lint_check.semantic_model import build_semantic_model, schema_diagnostics
from vgi_lint_check.semantic_schema import validate_instance


def dumped(value):
    return json.dumps(value, separators=(",", ":"))


def semantic_catalog(*objects):
    return catalog(
        schema("sales", tables=objects),
        tags={
            "vgi.semantic_catalog": dumped(
                {"catalog_id": "com.example.sales", "binding_key": "sales"}
            )
        },
    )


def rehome(worker, alias):
    worker.database = alias
    for model_schema in worker.schemas:
        model_schema.database = alias
        model_schema.id = ObjectId(alias, model_schema.id.kind, schema=model_schema.name)
        for relation in [*model_schema.tables, *model_schema.views]:
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
    return worker


def test_valid_semantic_model_merges_packed_and_native_members():
    orders = table(
        "sales",
        "orders",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "orders", "grain": ["order_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "order_id", "kind": "identifier", "column": "order_id"},
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
        columns=[
            col("sales", "orders", "order_id"),
            col(
                "sales",
                "orders",
                "amount",
                tags={"vgi.semantic_member": dumped({"member_id": "amount", "kind": "dimension"})},
            ),
        ],
    )
    worker = semantic_catalog(orders)
    assert schema_diagnostics(worker) == []
    model = build_semantic_model(worker)
    assert model.diagnostics == []
    assert set(model.entities[("com.example.sales", "orders")].members) == {
        "order_id",
        "amount",
        "revenue",
    }


def test_semantic_schema_rejects_raw_sql_and_unknown_properties():
    orders = table(
        "sales",
        "orders",
        tags={
            "vgi.semantic_entity": dumped(
                {"entity_id": "orders", "grain": ["order_id"], "sql": "select 1"}
            )
        },
    )
    errors = schema_diagnostics(semantic_catalog(orders))
    assert errors
    assert "Additional properties" in errors[0].message


def test_nested_struct_field_members_validate_against_the_top_level_column():
    places = table(
        "sales",
        "places",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "places", "grain": ["place_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "place_id", "kind": "identifier", "column": "id"},
                    {
                        "member_id": "bbox_xmin",
                        "kind": "dimension",
                        "column_path": ["bbox", "xmin"],
                        "data_type": "DOUBLE",
                    },
                ]
            ),
        },
        columns=[
            col("sales", "places", "id"),
            col("sales", "places", "bbox", dtype="STRUCT(xmin DOUBLE, xmax DOUBLE)"),
        ],
    )
    assert build_semantic_model(semantic_catalog(places)).diagnostics == []

    broken = json.loads(places.tags.raw["vgi.semantic_members"])
    broken[1]["column_path"] = ["bbox", "missing"]
    places.tags.raw["vgi.semantic_members"] = dumped(broken)
    assert {
        diagnostic.code for diagnostic in build_semantic_model(semantic_catalog(places)).diagnostics
    } == {"unknown_physical_column_path"}


def test_member_schema_distinguishes_literal_columns_from_nested_paths():
    assert (
        validate_instance(
            "member",
            {"member_id": "nested", "kind": "dimension", "column_path": ["bbox", "xmin"]},
        )
        == []
    )
    assert (
        validate_instance(
            "member",
            {"member_id": "literal", "kind": "dimension", "column": "bbox.xmin"},
        )
        == []
    )
    assert validate_instance(
        "member",
        {
            "member_id": "ambiguous",
            "kind": "dimension",
            "column": "bbox",
            "column_path": ["bbox", "xmin"],
        },
    )


def test_member_schema_accepts_source_arguments_and_rejects_competing_sources():
    member = {
        "member_id": "latitude",
        "kind": "dimension",
        "source_argument": "latitude",
        "data_type": "DOUBLE",
    }
    assert validate_instance("member", member) == []
    assert validate_instance("member", {**member, "column": "latitude"})


def test_packed_member_templates_expand_to_ordinary_members():
    events = table(
        "sales",
        "events",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "events", "grain": ["event_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "event_id", "kind": "identifier", "column": "id"},
                    {
                        "template_id": "scores",
                        "template": {
                            "kind": "dimension",
                            "data_type": "DOUBLE",
                            "unit": "percent",
                        },
                        "members": [
                            {"member_id": "quality", "column": "quality_score"},
                            {"member_id": "confidence", "column": "confidence_score"},
                        ],
                    },
                ]
            ),
        },
        columns=[
            col("sales", "events", "id"),
            col("sales", "events", "quality_score", dtype="DOUBLE"),
            col("sales", "events", "confidence_score", dtype="DOUBLE"),
        ],
    )
    worker = semantic_catalog(events)
    assert schema_diagnostics(worker) == []
    model = build_semantic_model(worker)
    assert model.diagnostics == []
    assert model.entities[("com.example.sales", "events")].members["quality"] == {
        "member_id": "quality",
        "kind": "dimension",
        "column": "quality_score",
        "data_type": "DOUBLE",
        "unit": "percent",
    }


def test_member_template_reports_invalid_expansion_and_duplicate_template_id():
    events = table(
        "sales",
        "events",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "events", "grain": ["event_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "event_id", "kind": "identifier", "column": "id"},
                    {
                        "template_id": "scores",
                        "template": {"data_type": "DOUBLE"},
                        "members": [{"member_id": "missing_kind", "column": "quality_score"}],
                    },
                    {
                        "template_id": "scores",
                        "template": {"kind": "dimension", "data_type": "DOUBLE"},
                        "members": [{"member_id": "quality", "column": "quality_score"}],
                    },
                ]
            ),
        },
        columns=[col("sales", "events", "id"), col("sales", "events", "quality_score")],
    )
    codes = {item.code for item in build_semantic_model(semantic_catalog(events)).diagnostics}
    assert {"duplicate_member_template", "invalid_expanded_member"} <= codes


def test_relationship_schema_requires_one_typed_list_collection_side():
    relationship = {
        "relationship_id": "com.example.segment_connector",
        "from": {"catalog_id": "com.example.maps", "entity_id": "segments"},
        "to": {"catalog_id": "com.example.maps", "entity_id": "connectors"},
        "from_cardinality": {"min": 0, "max": "many"},
        "to_cardinality": {"min": 0, "max": "many"},
        "predicate": [
            {
                "from_member": "connectors",
                "to_member": "id",
                "operator": "list_contains",
                "from_element_path": ["connector_id"],
            }
        ],
        "conditions": [{"side": "to", "member": "type", "value": "road"}],
    }
    assert validate_instance("relationship", relationship) == []
    del relationship["predicate"][0]["from_element_path"]
    assert validate_instance("relationship", relationship)


def test_relationship_conflict_and_bad_local_endpoint_are_reported():
    relationship = {
        "relationship_id": "com.example.order_customer",
        "from": {"catalog_id": "com.example.sales", "entity_id": "orders"},
        "to": {"catalog_id": "com.example.sales", "entity_id": "missing"},
        "from_cardinality": {"min": 0, "max": "many"},
        "to_cardinality": {"min": 1, "max": 1},
        "predicate": [{"from_member": "customer_id", "to_member": "customer_id"}],
    }
    orders = table(
        "sales",
        "orders",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "orders", "grain": ["order_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "order_id", "kind": "identifier", "column": "order_id"},
                    {"member_id": "customer_id", "kind": "dimension", "column": "customer_id"},
                ]
            ),
            "vgi.semantic_relationships": dumped([relationship]),
        },
    )
    codes = {
        diagnostic.code for diagnostic in build_semantic_model(semantic_catalog(orders)).diagnostics
    }
    assert "unresolved_local_entity" in codes


def test_semantic_consistency_rejects_relation_arguments_and_overstated_additivity():
    orders = table(
        "sales",
        "orders",
        tags={
            "vgi.semantic_entity": dumped(
                {
                    "entity_id": "orders",
                    "grain": ["order_id"],
                    "source": {"arguments": [{"argument": "region", "parameter": "region"}]},
                }
            ),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "order_id", "kind": "identifier", "column": "order_id"},
                    {
                        "member_id": "average_order",
                        "kind": "measure",
                        "aggregation": "avg",
                        "member": "order_id",
                        "additivity": "additive",
                    },
                ]
            ),
        },
        columns=[col("sales", "orders", "order_id")],
    )
    codes = {
        diagnostic.code for diagnostic in build_semantic_model(semantic_catalog(orders)).diagnostics
    }
    assert {"relation_source_arguments", "invalid_measure_additivity"} <= codes


def test_table_function_source_arguments_use_physical_signature_metadata():
    entity = func(
        "main",
        "events",
        "table",
        tags={
            "vgi.semantic_entity": dumped(
                {
                    "entity_id": "events",
                    "grain": ["event_id"],
                    "source": {
                        "arguments": [
                            {"argument": "since", "parameter": "start"},
                            {
                                "argument": "region",
                                "parameter": "region",
                                "required": False,
                            },
                        ]
                    },
                }
            ),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "event_id", "kind": "identifier", "column": "event_id"},
                    {
                        "member_id": "requested_region",
                        "kind": "dimension",
                        "source_argument": "region",
                        "data_type": "VARCHAR",
                    },
                ]
            ),
        },
        arguments=[
            arg("since", position=0, field_index=0, is_positional=True),
            arg("region", position=None, field_index=1, is_named=True, default='"all"'),
        ],
    )
    worker = catalog(
        schema("main", functions=[entity]),
        tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.events"})},
    )
    assert build_semantic_model(worker).diagnostics == []


def test_source_argument_dimension_requires_a_mapped_unambiguous_compatible_argument():
    entity = func(
        "main",
        "events",
        "table",
        tags={
            "vgi.semantic_entity": dumped(
                {
                    "entity_id": "events",
                    "grain": ["event_id"],
                    "source": {"arguments": [{"argument": "region", "parameter": "region"}]},
                }
            ),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "event_id", "kind": "identifier", "column": "event_id"},
                    {
                        "member_id": "requested_region",
                        "kind": "dimension",
                        "source_argument": "missing",
                        "data_type": "DOUBLE",
                    },
                ]
            ),
        },
        arguments=[arg("region", "VARCHAR", field_index=0, is_named=True)],
    )
    worker = catalog(
        schema("main", functions=[entity]),
        tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.events"})},
    )
    codes = {item.code for item in build_semantic_model(worker).diagnostics}
    assert "source_argument_member_missing" in codes

    members = json.loads(entity.tags.raw["vgi.semantic_members"])
    members[1]["source_argument"] = "region"
    entity.tags.raw["vgi.semantic_members"] = dumped(members)
    codes = {item.code for item in build_semantic_model(worker).diagnostics}
    assert "source_argument_member_type_mismatch" in codes

    definition = json.loads(entity.tags.raw["vgi.semantic_entity"])
    definition["source"]["arguments"] = []
    entity.tags.raw["vgi.semantic_entity"] = dumped(definition)
    codes = {item.code for item in build_semantic_model(worker).diagnostics}
    assert "source_argument_member_unmapped" in codes


def test_dynamic_units_validate_argument_mapping_and_advertised_choices():
    def codes(
        *,
        argument_name="temperature_unit",
        source_argument_name="temperature_unit",
        values=None,
        choices=None,
    ):
        entity = func(
            "main",
            "weather",
            "table",
            tags={
                "vgi.semantic_entity": dumped(
                    {
                        "entity_id": "weather",
                        "grain": ["reading_id"],
                        "source": {
                            "arguments": [
                                {
                                    "argument": source_argument_name,
                                    "parameter": "temperature_unit",
                                    "required": False,
                                }
                            ]
                        },
                    }
                ),
                "vgi.semantic_members": dumped(
                    [
                        {"member_id": "reading_id", "kind": "identifier", "column": "id"},
                        {
                            "member_id": "temperature",
                            "kind": "dimension",
                            "column": "temperature",
                            "unit_parameter": {
                                "argument": argument_name,
                                "values": values or {"celsius": "Cel", "fahrenheit": "[degF]"},
                            },
                        },
                    ]
                ),
            },
            arguments=[
                arg(
                    "temperature_unit",
                    field_index=0,
                    is_named=True,
                    default='"celsius"',
                    choices=choices,
                )
            ],
        )
        worker = catalog(
            schema("main", functions=[entity]),
            tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.weather"})},
        )
        return {item.code for item in build_semantic_model(worker).diagnostics}

    assert codes(choices='["celsius", "fahrenheit"]') == set()
    assert "unit_parameter_choices_incomplete" in codes(
        values={"celsius": "Cel"}, choices='["celsius", "fahrenheit"]'
    )
    assert "unit_parameter_argument_missing" in codes(argument_name="missing")
    assert "unit_parameter_source_unmapped" in codes(source_argument_name="different")


def test_unit_schema_rejects_empty_and_conflicting_units():
    member = {"member_id": "value", "kind": "dimension", "column": "value"}
    dynamic = {"unit_parameter": {"argument": "unit", "values": {"metric": "Cel"}}}
    assert validate_instance("member", {**member, "unit": "percent"}) == []
    assert validate_instance("member", {**member, **dynamic}) == []
    assert validate_instance("member", {**member, "unit": ""})
    assert validate_instance("member", {**member, "unit": "Cel", **dynamic})


def test_table_function_source_rejects_missing_metadata_overloads_and_varargs():
    def diagnostics(arguments):
        entity = func(
            "main",
            "events",
            "table",
            tags={
                "vgi.semantic_entity": dumped(
                    {
                        "entity_id": "events",
                        "grain": ["event_id"],
                        "source": {"arguments": [{"argument": "since", "parameter": "start"}]},
                    }
                ),
                "vgi.semantic_members": dumped(
                    [
                        {
                            "member_id": "event_id",
                            "kind": "identifier",
                            "column": "event_id",
                        }
                    ]
                ),
            },
            arguments=arguments,
        )
        worker = catalog(
            schema("main", functions=[entity]),
            tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.events"})},
        )
        return {item.code for item in build_semantic_model(worker).diagnostics}

    assert "missing_function_argument_metadata" in diagnostics([])
    assert "ambiguous_function_overload" in diagnostics(
        [
            arg("since", position=0, field_index=0, is_positional=True),
            arg("since", position=0, field_index=0, is_positional=True),
        ]
    )
    assert "unsupported_function_varargs" in diagnostics(
        [arg("since", field_index=0, is_varargs=True)]
    )


def test_table_function_detects_overload_count_and_unmapped_flattened_parameters():
    tags = {
        "vgi.semantic_entity": dumped({"entity_id": "events", "grain": ["event_id"]}),
        "vgi.semantic_members": dumped(
            [{"member_id": "event_id", "kind": "identifier", "column": "event_id"}]
        ),
    }
    first = func(
        "main",
        "events",
        "table",
        tags=tags,
        parameters=["since"],
        arguments=[arg("since", position=0, field_index=0, is_positional=True)],
    )
    second = func("main", "events", "table", tags=tags)
    overloaded = catalog(
        schema("main", functions=[first, second]),
        tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.events"})},
    )
    overloaded_codes = {item.code for item in build_semantic_model(overloaded).diagnostics}
    assert "ambiguous_function_overload" in overloaded_codes

    missing_metadata = func(
        "main", "events", "table", tags=tags, parameters=["since"], arguments=[]
    )
    incomplete = catalog(
        schema("main", functions=[missing_metadata]),
        tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.events"})},
    )
    incomplete_codes = {item.code for item in build_semantic_model(incomplete).diagnostics}
    assert "missing_function_argument_metadata" in incomplete_codes


def test_scalar_functions_cannot_host_semantic_entities():
    scalar = func(
        "main",
        "normalize_name",
        "scalar",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "names", "grain": ["name"]}),
            "vgi.semantic_members": dumped(
                [{"member_id": "name", "kind": "identifier", "column": "name"}]
            ),
        },
    )
    worker = catalog(
        schema("main", functions=[scalar]),
        tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.names"})},
    )
    assert "invalid_semantic_function_kind" in {
        item.code for item in build_semantic_model(worker).diagnostics
    }


def test_federation_reconciles_reciprocal_assertions_and_detects_duplicate_attachments():
    forward = {
        "relationship_id": "com.example.order_customer",
        "from": {"catalog_id": "com.example.sales", "entity_id": "orders"},
        "to": {"catalog_id": "com.example.crm", "entity_id": "customers"},
        "from_cardinality": {"min": 0, "max": "many"},
        "to_cardinality": {"min": 1, "max": 1},
        "predicate": [{"from_member": "customer_id", "to_member": "customer_id"}],
        "conditions": [{"side": "to", "member": "customer_id", "value": "preferred"}],
    }
    reverse = {
        **forward,
        "from": forward["to"],
        "to": forward["from"],
        "from_cardinality": forward["to_cardinality"],
        "to_cardinality": forward["from_cardinality"],
        "predicate": [{"from_member": "customer_id", "to_member": "customer_id"}],
        "conditions": [{"side": "from", "member": "customer_id", "value": "preferred"}],
    }
    orders = table(
        "main",
        "orders",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "orders", "grain": ["order_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {"member_id": "order_id", "kind": "identifier", "column": "order_id"},
                    {
                        "member_id": "customer_id",
                        "kind": "dimension",
                        "column": "customer_id",
                    },
                ]
            ),
            "vgi.semantic_relationships": dumped([forward]),
        },
        columns=[col("main", "orders", "order_id"), col("main", "orders", "customer_id")],
    )
    customers = table(
        "main",
        "customers",
        tags={
            "vgi.semantic_entity": dumped({"entity_id": "customers", "grain": ["customer_id"]}),
            "vgi.semantic_members": dumped(
                [
                    {
                        "member_id": "customer_id",
                        "kind": "identifier",
                        "column": "customer_id",
                    }
                ]
            ),
            "vgi.semantic_relationships": dumped([reverse]),
        },
        columns=[col("main", "customers", "customer_id")],
    )
    sales = rehome(
        catalog(
            schema("main", tables=[orders]),
            tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.sales"})},
        ),
        "sales_a",
    )
    crm = rehome(
        catalog(
            schema("main", tables=[customers]),
            tags={"vgi.semantic_catalog": dumped({"catalog_id": "com.example.crm"})},
        ),
        "crm",
    )
    graph = build_federated_semantic_model({"sales_a": sales, "crm": crm})
    edge = graph.relationships["com.example.order_customer"]
    assert edge.resolution_status == "resolved"
    assert edge.attestation == "corroborated"

    sales_b = rehome(deepcopy(sales), "sales_b")
    ambiguous = build_federated_semantic_model({"sales_a": sales, "sales_b": sales_b, "crm": crm})
    assert ambiguous.relationships["com.example.order_customer"].resolution_status == "ambiguous"
