"""``struct_fields.split_struct_fields``: a derived table's STRUCT column read field by field (BigQuery)."""

from collections import Counter

import pytest

import sqlglot

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.struct_fields import split_struct_fields

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"], "v": ["a", "s"]}

TYPES = {name: {column: "INT64" for column in columns} for name, columns in SCHEMA.items()}

DATASETS = [
    {"t": [], "u": []},
    {"t": [(1, None, 2), (1, None, 2), (None, None, None)], "u": [(1, 1, 1)]},
    {"t": [(0, 1, 2), (3, 2, None), (None, 3, 3), (0, 1, 2)], "u": [(None, 0, 0), (3, 3, 3), (0, None, 1)]},
    {"t": [(2, 2, 2)], "u": []},
]

def _proof(left: str, right: str, **kwargs):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False, **kwargs)

def _rule(sql: str) -> str:
    return split_struct_fields(sqlglot.parse_one(sql, read="bigquery"), SCHEMA).sql(dialect="bigquery")

def _unchanged(sql: str) -> bool:
    return _rule(sql) == sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")

def test_field_reads_become_columns_of_the_derived_table():
    sql = "SELECT s.f AS a, s.f AS a2, d.k AS k FROM (SELECT x.c AS k, STRUCT(x.a AS f, x.b AS g, x.c AS h) AS s FROM t AS x) AS d WHERE s.g > 1"
    assert _rule(sql) == (
        "SELECT d.kq_field0 AS a, d.kq_field0 AS a2, d.k AS k FROM (SELECT x.c AS k, x.a AS kq_field0, x.b AS kq_field1 FROM t AS x) AS d"
        " WHERE d.kq_field1 > 1"
    )
    # an unaliased derived table gets a fresh alias
    assert _rule("SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)") == (
        "SELECT kq_struct0.kq_field0 AS a FROM (SELECT x.a AS kq_field0 FROM t AS x) AS kq_struct0"
    )

def test_struct_reads_prove():
    assert _proof(
        "SELECT s.f AS a, s.g AS b FROM (SELECT STRUCT(x.a AS f, x.b AS g, x.c AS h) AS s FROM t AS x)",
        "SELECT x.a AS a, x.b AS b FROM t AS x",
    ).proven
    assert _proof(
        "SELECT s.f AS f, COUNT(*) AS n FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x) AS d WHERE s.g > 1 GROUP BY s.f",
        "SELECT x.a AS f, COUNT(*) AS n FROM t AS x WHERE x.b > 1 GROUP BY x.a",
    ).proven

def test_pair_that_differs_only_in_an_unread_with_table_proves():
    left = (
        "WITH c0 AS (SELECT s.f AS a, s.g AS b, s.h AS c FROM (SELECT STRUCT(x.a AS f, x.b AS g, x.c AS h) AS s FROM t AS x)),"
        " unused AS (SELECT x.a AS a FROM u AS x WHERE x.b > 1)"
        " SELECT x.a AS a, SUM(x.b) AS b FROM c0 AS x WHERE x.b <> 1 GROUP BY x.a"
    )
    assert _proof(left, left.replace("x.b > 1)", "x.b > 2)")).proven

def test_a_relation_named_like_the_struct_is_read_as_that_relation():
    # ``s.f`` is column f of the derived table s here, not a field of the struct
    sql = "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) AS d, (SELECT y.b AS f FROM u AS y) AS s"
    assert _unchanged(sql)
    assert not _proof(sql, "SELECT x.a AS a FROM t AS x, u AS y").proven
    assert _unchanged("WITH s AS (SELECT 1 AS f) SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)")

def test_struct_compared_whole_is_left_alone():
    # DISTINCT over the whole struct: keeping only the read field would merge rows that differ in g
    sql = "SELECT s.f AS a FROM (SELECT DISTINCT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)"
    assert _unchanged(sql)
    assert not _proof(sql, "SELECT DISTINCT x.a AS a FROM t AS x").proven
    union = "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x UNION DISTINCT SELECT STRUCT(x.b AS f) AS s FROM t AS x)"
    assert _unchanged(union)
    assert not _proof(union, "SELECT x.a AS a FROM t AS x UNION ALL SELECT x.b AS a FROM t AS x").proven

def test_field_names_are_case_insensitive_and_nested_structs_are_read_whole_or_not_at_all():
    assert _unchanged("SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f, x.b AS F) AS s FROM t AS x)")
    nested = "SELECT s.f.h AS a FROM (SELECT STRUCT(STRUCT(x.a AS h) AS f, x.b AS g) AS s FROM t AS x)"
    assert _unchanged(nested)
    assert not _proof(nested, "SELECT x.b AS a FROM t AS x").proven
    # reading the inner struct only through its outer field keeps it as one value
    assert "STRUCT" in _rule("SELECT s.g AS a, s.f AS w FROM (SELECT STRUCT(STRUCT(x.a AS h) AS f, x.b AS g) AS s FROM t AS x)")

def test_wrong_field_does_not_prove():
    assert not _proof("SELECT s.g AS a FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)", "SELECT x.a AS a FROM t AS x").proven

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s.f AS a, s AS w FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)",  # read whole
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) WHERE s IS NOT NULL",
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) AS d CROSS JOIN v",  # v has a column s
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f, x.b AS f) AS s FROM t AS x)",  # repeated field
        "SELECT s.z AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)",  # no such field
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a) AS s FROM t AS x)",  # unnamed field
        "SELECT s.f AS a FROM (SELECT STRUCT<f INT64>(x.a) AS s FROM t AS x)",  # a cast
        "SELECT s.f AS a FROM (SELECT IF(x.a > 0, STRUCT(x.a AS f), NULL) AS s FROM t AS x)",  # may be NULL
        "SELECT d.s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) AS d",  # deeper path
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) WHERE EXISTS (SELECT 1 FROM u WHERE u.a = s.f)",
        "SELECT * FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)",
        "SELECT s.f AS s FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x)",  # an output alias s
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x GROUP BY s)",
        "SELECT TO_JSON_STRING(d) AS j, s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) AS d",  # whole row
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f) AS s FROM t AS x) AS d JOIN u USING (a)",
        "SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s, COUNT(*) AS n FROM t AS x GROUP BY ALL) AS d",
    ],
)
def test_declined_shapes(sql):
    assert _unchanged(sql)
