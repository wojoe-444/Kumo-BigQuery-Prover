import sqlite3

import pytest

import sqlglot

from sqlglot import exp

from kumo_bq_prover.ported.ast_utils import FROM_KEY

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.projected_in_rules import normalize_projected_in

from kumo_bq_prover.ported.smt_equivalence import TableConstraints

NN = {"t": frozenset({"x"}), "u": frozenset({"y"})}

@pytest.mark.parametrize("sql, facts", [
    ("SELECT x IN (SELECT y FROM u) AS d FROM t", {"t": {"x"}}),
    ("SELECT x IN (SELECT y FROM u) AS d FROM t", {"u": {"y"}}),
    ("SELECT x IN (SELECT y FROM u LIMIT 1) AS d FROM t", NN),
    ("SELECT x IN (SELECT MAX(y) FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y FROM u GROUP BY y) AS d FROM t", NN),
    ("SELECT x IN (SELECT DISTINCT y FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y AS z FROM u ORDER BY z) AS d FROM t", NN),
    ("SELECT COUNT(*), (x) IN (SELECT y FROM u) AS d FROM t", NN),
    ("SELECT (x) IN (SELECT y FROM u) AS d FROM t GROUP BY x", NN),
    ("SELECT (x) IN (SELECT y FROM u) AS d FROM t GROUP BY ROLLUP(x)", NN),
    ("SELECT t.x IN (SELECT u.y FROM u) AS d FROM t LEFT JOIN u ON FALSE", NN),
    ("SELECT t.x IN (SELECT u.y FROM u) AS d FROM u RIGHT JOIN t ON FALSE", NN),
    ("SELECT x IN (SELECT y FROM u LEFT JOIN t ON FALSE) AS d FROM t", NN),
    ("SELECT (x, id) IN (SELECT y, id FROM u) AS d FROM t", NN),
    ("SELECT x + 0 IN (SELECT y FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y FROM u) AS d FROM t z(x,id)", NN),
])
def test_scope_or_nullability_uncertainty_is_refused(sql, facts):
    tree = sqlglot.parse_one(sql, read="mysql")
    assert normalize_projected_in(tree, facts) is None

def test_empty_global_aggregate_does_not_reuse_base_not_null():
    original = "SELECT COUNT(*), (x) IN (SELECT y FROM u) AS d FROM t"
    incorrect = "SELECT COUNT(*), EXISTS(SELECT 1 FROM u WHERE y=t.x) AS d FROM t"
    assert normalize_projected_in(sqlglot.parse_one(original, read="sqlite"), NN) is None
    assert not prove_equivalent_algebraic(
        original, incorrect, schema={"t": ["x"], "u": ["y"]},
        constraints={name: TableConstraints(not_null=cols) for name, cols in NN.items()},
        compare_names=False, dialect="sqlite",
    ).proven
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE t(x INTEGER NOT NULL)")
        db.execute("CREATE TABLE u(y INTEGER NOT NULL)")
        db.execute("INSERT INTO u VALUES (1)")
        assert db.execute(original).fetchall() == [(0, None)]
        assert db.execute(incorrect).fetchall() == [(0, 0)]

def test_identity_projection_is_kept_under_aggregate_ancestor():
    original = "WITH q AS (SELECT x AS x FROM t) SELECT COUNT(*), x IN (SELECT y FROM u) AS d FROM q"
    incorrect = "SELECT COUNT(*), EXISTS(SELECT 1 FROM u WHERE y=q.x) AS d FROM t q"
    schema = {"t": ["x"], "u": ["y"]}
    normalized = normalize(original, schema=schema, not_null=NN, dialect="sqlite")
    assert " IN " in normalized
    assert not prove_equivalent_algebraic(
        original, incorrect, schema=schema,
        constraints={name: TableConstraints(not_null=cols) for name, cols in NN.items()},
        compare_names=False, dialect="sqlite",
    ).proven
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE t(x INTEGER NOT NULL)")
        db.execute("CREATE TABLE u(y INTEGER NOT NULL)")
        db.execute("INSERT INTO u VALUES (1)")
        assert db.execute(original).fetchall() == db.execute(normalized).fetchall() == [(0, None)]
        assert db.execute(incorrect).fetchall() == [(0, 0)]

def test_identity_column_aliases_unlock_cte_membership_without_capture():
    left = "WITH q AS (SELECT id, X AS X FROM t) SELECT id, X IN (SELECT X FROM q WHERE id<3) AS d FROM q"
    right = "SELECT t.id, EXISTS(SELECT 1 FROM t u WHERE u.id<3 AND u.x=t.x) AS d FROM t"
    result = prove_equivalent_algebraic(
        left, right, schema={"t": ["id", "x"]},
        constraints={"t": TableConstraints(not_null=frozenset({"x"}))},
        compare_names=False, dialect="mysql",
    )
    assert result.proven

@pytest.mark.parametrize("item", ["x AS other", "x AS X", "`x` AS x", "x AS `x`"])
def test_nonidentity_or_differently_quoted_alias_is_kept(item):
    tree = sqlglot.parse_one(f"SELECT d.x FROM (SELECT {item} FROM t) AS d", read="mysql")
    assert normalize_projected_in(tree.args[FROM_KEY].this.this, NN) is None
