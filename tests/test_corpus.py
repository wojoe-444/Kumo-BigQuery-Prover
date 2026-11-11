import json
import pathlib

import sqlglot

CORPUS = json.loads((pathlib.Path(__file__).parent.parent / "benchmarks/bq_shapes/cases.json").read_text())


def test_every_statement_parses_as_bigquery():
    statements = list(CORPUS["tables"].values())
    for case in CORPUS["cases"]:
        statements += [case["left"], case["right"]]
    for sql in statements:
        sqlglot.parse_one(sql, read="bigquery")


def test_cases_are_well_formed_and_cover_the_target_shapes():
    ids = [case["id"] for case in CORPUS["cases"]]
    assert len(ids) == len(set(ids))
    for case in CORPUS["cases"]:
        assert case["label"] in {"equivalent", "not_equivalent"}
        assert set(case["tables"]) <= set(CORPUS["tables"])
    shapes = {shape for case in CORPUS["cases"] for shape in case["shapes"]}
    assert {"STRUCT", "ARRAY", "QUALIFY", "CTE", "incremental"} <= shapes
    labels = {case["label"] for case in CORPUS["cases"]}
    assert labels == {"equivalent", "not_equivalent"}
