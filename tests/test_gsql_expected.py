from datetime import date
from decimal import Decimal

import pytest

from kumo_bq_prover.gsql_expected import ExpectedError, NanoPrecision, UnorderedList, parse_expected


def test_rows_of_a_struct():
    expected = parse_expected('ARRAY<STRUCT<a INT64, b STRING>>[known order:{1, "x"}, {NULL, "y"}]')
    assert expected.kind == "rows" and not expected.unordered
    assert expected.type == ("STRUCT", (("a", ("INT64",)), ("b", ("STRING",))))
    assert expected.rows == [(1, "x"), (None, "y")]


def test_unknown_order_and_nested_array_types():
    expected = parse_expected("ARRAY<STRUCT<ARRAY<>>>[unknown order:{ARRAY<INT64>[unknown order:2, 1]}]")
    assert expected.unordered
    (row,) = expected.rows
    assert isinstance(row[0], UnorderedList) and list(row[0]) == [2, 1]


def test_scalar_types():
    expected = parse_expected("ARRAY<STRUCT<NUMERIC, DATE, DOUBLE, BOOL>>[{1.50, 2020-12-31, 2.5, true}]")
    assert expected.rows == [(Decimal("1.50"), date(2020, 12, 31), 2.5, True)]


def test_error_with_code():
    expected = parse_expected("ERROR: generic::out_of_range: double overflow: 1e+300 / 1e-300")
    assert expected.kind == "error" and expected.code == "out_of_range"


def test_nanosecond_digits_are_refused_not_rounded():
    with pytest.raises(NanoPrecision):
        parse_expected("ARRAY<STRUCT<TIME>>[{12:34:56.123456789}]")


def test_unreadable_text_is_an_error():
    with pytest.raises(ExpectedError):
        parse_expected("not a result")
