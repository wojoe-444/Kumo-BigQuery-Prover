import json
import pathlib

import sqlglot

PAIRS = pathlib.Path(__file__).parent.parent / "benchmarks/pairs"


def cases(name):
    return [json.loads(line) for line in (PAIRS / f"{name}.jsonl").read_text().splitlines() if line]


def test_every_pair_parses_as_bigquery_and_ids_are_unique():
    for name in ("documented-rewrites", "numeric-traps"):
        rows = cases(name)
        assert len({r["id"] for r in rows}) == len(rows)
        for row in rows:
            sqlglot.parse_one(row["left"], read="bigquery")
            sqlglot.parse_one(row["right"], read="bigquery")


def test_sizes_match_the_recorded_corpora():
    assert len(cases("documented-rewrites")) == 25
    assert len(cases("numeric-traps")) == 112


def test_cases_that_real_bigquery_contradicts_carry_a_note():
    notes = {r["id"] for name in ("documented-rewrites", "numeric-traps") for r in cases(name) if "bigquery_note" in r}
    assert {"R012b-09", "numeric-literal-rounding", "numeric-literal-below-half", "cast-versus-round", "float-literal-overflow"} <= notes
