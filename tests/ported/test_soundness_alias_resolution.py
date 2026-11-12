"""A bare name in ORDER BY or QUALIFY that is both a select alias and a source column.

BigQuery reads the alias in ORDER BY (inside expressions and windows too) and everywhere in QUALIFY; it reads the
source column in WHERE and in the select list. another engine reads the source column everywhere except a bare ORDER BY key.
The provers used to read the source column inside an ORDER BY expression and inside a QUALIFY window on every
dialect, which proved pairs BigQuery answers differently. Every BigQuery expectation below was run there on
2026-11-10 with inline values (zero bytes billed); the another engine ones run in ``test_other_engine_reads_the_source_column``.
"""

import pytest

pytest.importorskip("z3")

from sqlglot import exp, parse_one  # noqa: E402

from kumo_bq_prover.ported import clause_alias_reads as R  # noqa: E402

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402

from kumo_bq_prover.ported.ast_utils import UnmodeledConstruct  # noqa: E402

from kumo_bq_prover.ported.equivalence import prove_equivalent  # noqa: E402

from kumo_bq_prover.ported.smt_equivalence import prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["id", "a", "b"]}

TYPES = {"t": {"id": "INT64", "a": "INT64", "b": "INT64"}}

DIFFERENT = [
    # ORDER BY: a name that is one output's alias and another output's expression
    ("SELECT a AS b, b AS a FROM t ORDER BY a LIMIT 1", "SELECT a AS b, b AS a FROM t ORDER BY t.a LIMIT 1"),
    # ORDER BY: the alias inside an expression
    ("SELECT b AS a FROM t ORDER BY a + 1 LIMIT 1", "SELECT b AS a FROM t ORDER BY t.a + 1 LIMIT 1"),
    ("SELECT id, b AS a FROM t ORDER BY -a LIMIT 1", "SELECT id, b AS a FROM t ORDER BY -t.a LIMIT 1"),
    # ORDER BY: a window that reads the alias
    ("SELECT id, b AS a FROM t ORDER BY RANK() OVER (ORDER BY a) LIMIT 1", "SELECT id, b AS a FROM t ORDER BY RANK() OVER (ORDER BY t.a) LIMIT 1"),
    # QUALIFY: the alias as a window's ORDER BY, PARTITION BY and argument
    ("SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1", "SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY t.a) = 1"),
    ("SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY a) = 2", "SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY t.a) = 2"),
    ("SELECT id, b AS a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY a DESC) = 1", "SELECT id, b AS a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY t.a DESC) = 1"),
    ("SELECT id, b AS a FROM t QUALIFY a = MAX(a) OVER ()", "SELECT id, b AS a FROM t QUALIFY a = MAX(t.a) OVER ()"),
    ("SELECT -a AS a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", "SELECT -a AS a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY t.a) = 1"),
    ("SELECT id, a + 10 AS a FROM t QUALIFY SUM(a) OVER () > 30", "SELECT id, a + 10 AS a FROM t QUALIFY SUM(t.a) OVER () > 30"),
    # QUALIFY: the alias outside any window
    ("SELECT id, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0", "SELECT id, b AS a FROM t WHERE a = 2 QUALIFY COUNT(*) OVER () > 0"),
    ("SELECT a AS x, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0", "SELECT a AS x, b AS a FROM t QUALIFY t.a = 2 AND COUNT(*) OVER () > 0"),
    # the select list reads the source column, so its window is not the QUALIFY window of the same text
    ("SELECT id, b AS a, RANK() OVER (ORDER BY a) AS r FROM t", "SELECT id, b AS a, RANK() OVER (ORDER BY b) AS r FROM t"),
]

EQUIVALENT_BIGQUERY = [
    ("SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1", "SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY b) = 1"),
    ("SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY a) = 2", "SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY b) = 2"),
    ("SELECT id, b AS a FROM t QUALIFY a = MAX(a) OVER ()", "SELECT id, b AS a FROM t QUALIFY b = MAX(b) OVER ()"),
    ("SELECT id, a + 10 AS a FROM t QUALIFY SUM(a) OVER () > 30", "SELECT id, a + 10 AS a FROM t QUALIFY SUM(t.a + 10) OVER () > 30"),
    ("SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY n = 1", "SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1"),
]

@pytest.mark.parametrize("left,right", EQUIVALENT_BIGQUERY)
def test_bigquery_alias_reads_are_proven(left, right):
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery").proven

CAST_IS_NOT_THE_COLUMN = [
    ("SELECT id, CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, CAST(b AS STRING) FROM t ORDER BY 2 LIMIT 1"),
    ("SELECT id, CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, CAST(b AS STRING) FROM t ORDER BY CAST(b AS STRING) LIMIT 1"),
    ("SELECT id, SAFE_CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, SAFE_CAST(b AS STRING) FROM t ORDER BY 2 LIMIT 1"),
    ("SELECT id, (CAST(b AS STRING)) FROM t ORDER BY b LIMIT 1", "SELECT id, (CAST(b AS STRING)) FROM t ORDER BY 2 LIMIT 1"),
]

@pytest.mark.parametrize("left,right", CAST_IS_NOT_THE_COLUMN)
@pytest.mark.parametrize("types", [TYPES, None], ids=["typed", "untyped"])
def test_order_by_a_column_is_not_order_by_an_unaliased_cast_of_it(left, right, types):
    assert not prove_equivalent(left, right).proven
    assert not prove_equivalent_smt(left, right, schema=SCHEMA, types=types, dialect="bigquery").proven
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types, dialect="bigquery").proven

def test_shadowing_aliases():
    def names(sql):
        return {name: (value.sql() if value is not None else None) for name, value in R.shadowing_aliases(parse_one(sql, read="bigquery")).items()}

    assert names("SELECT b AS a, a AS a2, id FROM t") == {"a": "b", "a2": "a"}
    assert names("SELECT a AS a, t.b AS b FROM t") == {}  # the alias is the column of that name
    assert names("SELECT u.a AS a FROM t JOIN u ON t.id = u.id") == {"a": "u.a"}  # one of two columns called a
    assert names("SELECT b AS a, id AS a FROM t") == {"a": None}

def test_may_be_source_column():
    def may(sql, name, schema=SCHEMA):
        return R.may_be_source_column(parse_one(sql, read="bigquery").find(exp.Select), name, schema)

    assert may("SELECT 1 FROM t", "a") and not may("SELECT 1 FROM t", "n")
    assert may("SELECT 1 FROM unknown_table", "n")
    assert may("SELECT 1 FROM t", "n", schema=None)
    assert not may("SELECT 1 FROM (SELECT id, a AS x FROM t) AS d", "a") and may("SELECT 1 FROM (SELECT id, a AS x FROM t) AS d", "x")
    assert may("SELECT 1 FROM (SELECT * FROM t) AS d", "n")
    assert not may("WITH c AS (SELECT id FROM t) SELECT 1 FROM c", "a")
    assert may("SELECT 1 FROM t JOIN (SELECT id AS n FROM t) AS d ON t.id = d.n", "n")
