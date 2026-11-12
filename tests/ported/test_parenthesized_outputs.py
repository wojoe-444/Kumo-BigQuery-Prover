"""``SELECT (t.a)`` outputs the column ``a`` under its own name (a false proof came from losing it)."""

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import TableConstraints

SCHEMA = {"t": ["a", "b"], "u": ["a", "c"]}

CONSTRAINTS = {"t": TableConstraints(not_null=frozenset({"a"}), keys=(("a",),))}

def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, constraints=CONSTRAINTS, compare_names=False, dialect="mysql").proven

def test_the_derived_output_keeps_the_column_name():
    text = normalize("SELECT d.a FROM (SELECT DISTINCT (x.a) FROM t AS x JOIN u ON u.a = x.b) AS d", schema=SCHEMA, dialect="mysql")
    assert "kqc" not in text
    assert proved(
        "SELECT d.a FROM (SELECT DISTINCT (x.a) FROM t AS x JOIN u ON u.a = x.b) AS d",
        "SELECT d.a FROM (SELECT DISTINCT x.a FROM t AS x JOIN u ON u.a = x.b) AS d",
    )
    assert proved("SELECT ((x.a)), (x.b) FROM t AS x", "SELECT x.a, x.b FROM t AS x")

def test_a_read_of_the_parenthesized_output_is_not_a_correlated_read():
    # the inner a is the derived table's column, not the outer t.a: this is "a not among the joined rows", not "no joined row"
    membership = "SELECT a FROM t WHERE a NOT IN (SELECT a FROM (SELECT DISTINCT (x.a) FROM t AS x JOIN u ON u.a = x.b) AS d)"
    assert not proved(membership, "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM t AS x JOIN u ON u.a = x.b)")
    assert proved(membership, membership.replace("(x.a)", "x.a"))
    with_cte = "WITH d AS (SELECT DISTINCT (x.a) FROM t AS x JOIN u ON u.a = x.b) SELECT a FROM t WHERE a NOT IN (SELECT a FROM d)"
    assert not proved(with_cte, "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM t AS x JOIN u ON u.a = x.b)")
