import json
import pathlib
import re

import sqlglot

PAIRS = pathlib.Path(__file__).parent.parent / "benchmarks/pairs"


def cases(name):
    return [json.loads(line) for line in (PAIRS / f"{name}.jsonl").read_text().splitlines() if line]


def test_every_pair_parses_as_bigquery_and_ids_are_unique():
    for name in ("documented-rewrites", "numeric-traps", "unsafe-rewrites", "sample-databases"):
        rows = cases(name)
        assert len({r["id"] for r in rows}) == len(rows)
        for row in rows:
            sqlglot.parse_one(row["left"], read="bigquery")
            sqlglot.parse_one(row["right"], read="bigquery")


def test_sizes_match_the_recorded_corpora():
    assert len(cases("documented-rewrites")) == 25
    assert len(cases("numeric-traps")) == 112
    assert len(cases("unsafe-rewrites")) == 560
    assert len(cases("sample-databases")) == 286
    assert len(cases("jaffle-shop-refactors")) == 15


def test_jaffle_refactors_cover_every_model_and_name_their_refs():
    for case in cases("jaffle-shop-refactors"):
        assert case["label"] in ("equivalent", "not_equivalent")
        names = [name for name, _ in case["before"]]
        assert {"customers", "orders"} <= set(names)
        for _name, sql in case["before"] + case["after"]:
            assert "{{" not in sql and "{%" not in sql
            sqlglot.parse_one(re.sub(r"@@(\w+)@@", r"\1", sql), read="bigquery")


def test_labels_changed_after_a_real_bigquery_check_say_so():
    numeric = {r["id"]: r for r in cases("numeric-traps")}
    assert numeric["numeric-versus-float-literal"]["label"] == "equivalent"
    assert "Label changed" in numeric["numeric-versus-float-literal"]["bigquery_note"]
    sample = {r["id"]: r for r in cases("sample-databases")}
    assert sample["sk-fk-chain-join-elimination"]["label"] == "not_equivalent"
    assert sample["sk-fk-chain-join-elimination-aliased"]["label"] == "equivalent"


def test_cases_that_real_bigquery_contradicts_carry_a_note():
    notes = {r["id"] for name in ("documented-rewrites", "numeric-traps") for r in cases(name) if "bigquery_note" in r}
    assert {"R012b-09", "numeric-literal-rounding", "numeric-literal-below-half", "cast-versus-round", "float-literal-overflow"} <= notes
