"""Concatenations and one-column grouping sets are spelled the way they print (``kumo_bq_prover.ported.printed_spelling``)."""

import random

import pytest

import sqlglot

from sqlglot import exp

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.ast_utils import faithful_sql

from kumo_bq_prover.ported.printed_spelling import spell_as_printed

SCHEMA = {"t": ["a", "b", "c", "k"], "u": ["a", "b", "c", "k"]}

TYPES = {name: {"a": "TEXT", "b": "TEXT", "c": "TEXT", "k": "INT"} for name in SCHEMA}

def proved(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven

def concat(*names, **flags):
    return exp.Concat(expressions=[exp.column(n) for n in names], **flags)

def test_an_operator_operand_keeps_its_grouping():
    inner = exp.EQ(this=exp.column("a"), expression=exp.column("b"))
    tree = exp.select(exp.alias_(exp.Concat(expressions=[inner, exp.column("c")]), "n")).from_("t")
    assert faithful_sql(spell_as_printed(tree, "postgres"), "postgres") == "SELECT (a = b) || c AS n FROM t"
    nested = exp.select(exp.alias_(exp.Concat(expressions=[exp.column("a"), exp.Upper(this=concat("b", "c"))]), "n")).from_("t")
    assert faithful_sql(spell_as_printed(nested, "postgres"), "postgres") == "SELECT a || UPPER(b || c) AS n FROM t"

def test_other_concats_and_dialects_stay():
    skipping = exp.select(exp.alias_(concat("a", "b", coalesce=True), "n")).from_("t")  # PostgreSQL's CONCAT(): NULL is ''
    assert spell_as_printed(skipping, "postgres") is skipping
    single = exp.select(exp.alias_(concat("a"), "n")).from_("t")
    assert spell_as_printed(single, "postgres") is single
    plain = exp.select(exp.alias_(concat("a", "b"), "n")).from_("t")
    for dialect in ("bigquery", "mysql", "oracle", "snowflake", "spark", "tsql", ""):
        assert spell_as_printed(plain, dialect) is plain

TARGETS = [
    ("SELECT a || b AS n FROM t", "SELECT a || b AS n FROM t"),
    ("SELECT COALESCE(a, '') || ', ' || COALESCE(b, '') AS n FROM t ORDER BY k LIMIT 10", "SELECT COALESCE(x.a, '') || ', ' || COALESCE(x.b, '') AS n FROM t AS x ORDER BY x.k LIMIT 10"),
    ("SELECT a || (b || c) AS n FROM t", "SELECT x.a || (x.b || x.c) AS n FROM t AS x"),
    ("SELECT 'store' || t.a AS id, SUM(t.k) AS s FROM t, u WHERE t.k = u.k GROUP BY t.a", "SELECT 'store' || t.a AS id, SUM(t.k) AS s FROM t JOIN u ON t.k = u.k GROUP BY t.a"),
    ("SELECT k FROM t WHERE a || b = 'xy'", "SELECT k FROM (SELECT * FROM t WHERE a || b = 'xy') AS d"),
]

ROWS = {"t": [("x", "y", "z", 1), ("x", None, "z", 2), (None, None, None, 3), ("", "y", "", 4), ("x", "y", "z", 1)]}

NEAR_MISSES = [
    # CONCAT() skips a NULL argument, || is NULL
    ("SELECT a || b AS n FROM t", "SELECT CONCAT(a, b) AS n FROM t"),
    ("SELECT a || b AS n FROM t", "SELECT COALESCE(a, '') || COALESCE(b, '') AS n FROM t"),
    # the order of the operands, and which operands
    ("SELECT a || b AS n FROM t", "SELECT b || a AS n FROM t"),
    ("SELECT a || b || c AS n FROM t", "SELECT a || c AS n FROM t"),
    ("SELECT a || b AS n FROM t", "SELECT a || c AS n FROM t"),
    # a NULL operand makes the filter reject the row
    ("SELECT k FROM t WHERE a || b IS NULL", "SELECT k FROM t WHERE a IS NULL"),
    ("SELECT k FROM t WHERE a || b = 'xy'", "SELECT k FROM t WHERE a = 'x'"),
]

NORMALIZED_QUERIES = [
    "SELECT a || b AS n, k FROM t",
    "SELECT a || (b || c) AS n, (a || b) || c AS m FROM t",
    "SELECT COALESCE(a, '') || ', ' || COALESCE(b, '') AS n FROM t",
    "SELECT k FROM t WHERE a || b IS NULL OR 'p' || c = 'pz'",
    "SELECT a || '-' || b AS n, COUNT(*) AS c FROM t GROUP BY a || '-' || b",
    "SELECT t.k FROM t JOIN u ON t.a || t.b = u.a || u.b",
    "SELECT CONCAT(a, b) || c AS n, CONCAT(a || b, c) AS m FROM t",
]

def random_rows(rng):
    def table(size):
        texts = [None, "", "x", "y", "xy", "é"]
        rows = [(rng.choice(texts), rng.choice(texts), rng.choice(texts), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(6), "u": table(4)}

def test_a_rollup_the_prover_cannot_split_still_prints_faithfully():
    # a window reads every grouping set's rows, so the rollup stays GROUPING SETS; it used to be refused as unprintable
    windowed = "SELECT a, b, SUM(k) AS s, RANK() OVER (PARTITION BY a ORDER BY SUM(k)) AS r FROM t GROUP BY ROLLUP (a, b)"
    out = normalize(windowed, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "GROUP BY GROUPING SETS ((a, b), (a), ())" in out
    result = prove_equivalent_algebraic(windowed, windowed.replace("ROLLUP (a, b)", "a, b"), schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False)
    assert not result.proven and "printed faithfully" not in result.reason
