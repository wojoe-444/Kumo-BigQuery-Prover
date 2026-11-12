from kumo_bq_prover import gsql_compliance as g
from kumo_bq_prover.emulator import QueryResult
from kumo_bq_prover.gsql_expected import parse_expected


def result(fields, rows):
    return QueryResult(schema={"fields": fields}, rows=[], raw_rows=[{"f": [{"v": v} for v in row]} for row in rows])


INT = {"name": "a", "type": "INTEGER", "mode": "NULLABLE"}
FLOAT = {"name": "f", "type": "FLOAT", "mode": "NULLABLE"}
ARR = {"name": "arr", "type": "INTEGER", "mode": "REPEATED"}


def test_equal_rows_pass():
    expected = parse_expected("ARRAY<STRUCT<a INT64>>[{1}, {2}]")
    assert g.compare(expected, result([INT], [["1"], ["2"]])) == ("pass", "")


def test_known_order_is_checked_and_unknown_order_is_not():
    ordered = parse_expected("ARRAY<STRUCT<a INT64>>[known order:{1}, {2}]")
    unordered = parse_expected("ARRAY<STRUCT<a INT64>>[unknown order:{1}, {2}]")
    swapped = result([INT], [["2"], ["1"]])
    assert g.compare(ordered, swapped)[0] == "wrong"
    assert g.compare(unordered, swapped)[0] == "pass"


def test_a_null_array_and_an_empty_array_are_the_same_result():
    expected = parse_expected("ARRAY<STRUCT<ARRAY<INT64>>>[{NULL}]")
    assert g.compare(expected, result([ARR], [[[]]]))[0] == "pass"


def test_signed_zero_is_not_scored_but_ulps_are():
    expected = parse_expected("ARRAY<STRUCT<f DOUBLE>>[{0.0}]")
    assert g.compare(expected, result([FLOAT], [["-0.0"]]))[0] == "pass"
    near = parse_expected("ARRAY<STRUCT<f DOUBLE>>[{0.1}]")
    assert g.compare(near, result([FLOAT], [["0.30000000000000004"]]))[0] == "wrong"


def test_different_rows_and_different_types_are_wrong():
    expected = parse_expected("ARRAY<STRUCT<a INT64>>[{1}]")
    assert g.compare(expected, result([INT], [["2"]]))[0] == "wrong"
    assert g.compare(expected, result([FLOAT], [["1"]]))[0] == "wrong"


def test_googlesql_narrow_types_match_wide_ones_when_checking_tables():
    expected = parse_expected("ARRAY<STRUCT<a UINT64>>[{1}]")
    assert g.compare(expected, result([INT], [["1"]]), loose=True)[0] == "pass"


def test_claimed_subset_is_the_fixed_one():
    counts = {split: sum(c.claimed for c in g.cases(split)) for split in ("dev", "heldout")}
    assert counts == {"dev": 3149, "heldout": 1599}


def test_split_is_by_file_name():
    assert {g.split_of(c.file) for c in g.cases("dev")[:200]} == {"dev"}
    assert {g.split_of(c.file) for c in g.cases("heldout")[:200]} == {"heldout"}
