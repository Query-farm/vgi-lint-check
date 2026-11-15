"""Overloads that share one examples tag are executed once."""

import json

from tests import fixtures as F


def test_overloads_with_the_same_examples_tag_are_yielded_once():
    ex = json.dumps([{"description": "d", "sql": "SELECT 1"}])
    a = F.func("main", "f", tags={"vgi.executable_examples": ex}, executable_examples=["x"])
    b = F.func("main", "f", tags={"vgi.executable_examples": ex}, executable_examples=["x"])
    c = F.func(
        "main",
        "f",
        tags={"vgi.executable_examples": json.dumps([{"description": "e", "sql": "SELECT 2"}])},
        executable_examples=["y"],
    )
    cat = F.catalog(F.schema("main", functions=[a, b, c]))
    hosts = [h for h, exs, _ in cat.iter_executable_example_hosts() if exs]
    assert len(hosts) == 2
