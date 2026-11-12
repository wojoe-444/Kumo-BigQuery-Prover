"""MySQL's ``CAST(EXISTS(...) AS SIGNED)`` output is the ``EXISTS`` (``kumo_bq_prover.ported.mysql_exists_casts``)."""

from collections import Counter

import random

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.mysql_exists_casts import drop_mysql_exists_cast

SCHEMA = {"t": ["a", "b", "s"], "u": ["a", "b"]}

TYPES = {"t": {"a": "INT", "b": "INT", "s": "DOUBLE"}, "u": {"a": "INT", "b": "INT"}}

DDL = {"t": "a BIGINT, b BIGINT, s DOUBLE", "u": "a BIGINT, b BIGINT"}

EXISTS = "EXISTS(SELECT 1 FROM u WHERE u.a = t.a)"

SUBQUERIES = [
    "SELECT 1 FROM u WHERE u.a = t.a",
    "SELECT 1 FROM u WHERE u.a > t.b OR u.b IS NULL",
    "SELECT u.b FROM u",
    "SELECT 1 FROM u WHERE u.a = NULL",
    "SELECT NULL FROM u WHERE u.b = t.b",
    "SELECT COUNT(*) FROM u WHERE u.a = t.a",  # one row always
    "SELECT u.a FROM u WHERE u.b = t.b UNION ALL SELECT u.b FROM u WHERE u.a = t.a",
]

def dropped(sql, dialect="mysql"):
    out = drop_mysql_exists_cast(sqlglot.parse_one(sql, read=dialect), dialect)
    return out.sql(dialect=dialect) if out is not None else None

def proved(left, right, dialect="mysql"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False, dialect=dialect).proven

def mysql_values(rows):
    """Rows as MySQL would show them: a truth value is the integer 1 or 0."""

    return Counter(tuple(int(v) if isinstance(v, bool) else v for v in row) for row in rows)

@pytest.mark.parametrize("subquery", SUBQUERIES)
def test_cast_of_an_exists_output_is_dropped(subquery):
    expected = sqlglot.parse_one(f"SELECT t.b, EXISTS({subquery}) AS f FROM t", read="mysql").sql(dialect="mysql")
    assert dropped(f"SELECT t.b, CAST(EXISTS({subquery}) AS SIGNED) AS f FROM t") == expected
    assert dropped(f"SELECT t.b, CAST((EXISTS({subquery})) AS SIGNED INTEGER) AS f FROM t") == expected

def test_the_cast_and_the_bare_exists_are_proved_equal_on_mysql():
    assert proved(f"SELECT t.b, CAST({EXISTS} AS SIGNED) AS f FROM t", f"SELECT t.b, {EXISTS} AS f FROM t")
    assert proved(f"SELECT CAST({EXISTS} AS SIGNED) FROM t WHERE t.b > 1", f"SELECT {EXISTS} FROM t WHERE t.b > 1")

def test_the_recorded_decision_on_comparisons_is_kept():
    # true on MySQL, and deliberately not proved: this module only reads EXISTS
    assert not proved("SELECT t.a = 1 AS f FROM t", "SELECT CAST(t.a = 1 AS SIGNED) AS f FROM t")

NEAR_MISSES = [
    # EXISTS is never NULL; IN over a NULL is
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f FROM t", "SELECT t.a IN (SELECT u.a FROM u) AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(None, 1)]}, "mysql"),
    # the cast of a number is not the number: 2.5 becomes a whole number
    ("SELECT CAST(t.s AS SIGNED) AS f FROM t", "SELECT t.s AS f FROM t", {"t": [(1, 1, 2.5)]}, "mysql"),
    # a cast inside arithmetic is not the output: 2 against 1
    (f"SELECT CAST({EXISTS} AS SIGNED) + 1 AS f FROM t", f"SELECT {EXISTS} AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 1)]}, "mysql"),
    # another subquery under the cast
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f FROM t", "SELECT EXISTS(SELECT 1 FROM u WHERE u.b = t.a) AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 2)]}, "mysql"),
    # outside MySQL the integer 1 is not the boolean TRUE
    (f"SELECT CAST({EXISTS} AS INT64) AS f FROM t", f"SELECT {EXISTS} AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 1)]}, "bigquery"),
]
