"""GROUP BY ALL infers its keys from the select list (determinism and robustness audit, DR-01).

Both provers used to ignore the ``all`` flag: an aggregate-only ``GROUP BY ALL`` read as a grouped query with no
row on empty input, so ``COUNT(*) .. GROUP BY ALL`` was proved equal to ``GROUP BY a`` and refuted against the
global ``COUNT(*)``. The keys are now spelled out (as positions) before proving; shapes whose keys depend on the
engine or on outer scopes are declined.
"""

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.ast_utils import UnmodeledConstruct, expand_group_by_all

from kumo_bq_prover.ported.smt_equivalence import SmtStatus, prove_equivalent_smt

SCHEMA = {"t": ["a", "b", "c"]}

PROVERS = (prove_equivalent_smt, prove_equivalent_algebraic)

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS k, COUNT(*) AS n FROM t GROUP BY ALL",  # only a constant: a key in another engine, not in BigQuery
        "SELECT 1 AS k FROM t GROUP BY ALL",
        "SELECT * FROM t GROUP BY ALL",
        "SELECT a, SUM(b) OVER () AS w FROM t GROUP BY ALL",
        "SELECT a, (SELECT 1) AS s, COUNT(*) AS n FROM t GROUP BY ALL",
        "SELECT a, MY_UDF(b) AS u, COUNT(*) AS n FROM t GROUP BY ALL",
        "SELECT b, COUNT(*) + a AS n FROM t GROUP BY ALL",
        "SELECT a FROM t WHERE a IN (SELECT b FROM t GROUP BY ALL)",  # may read outer columns
    ],
)
def test_group_by_all_shapes_that_are_declined(sql):
    with pytest.raises(UnmodeledConstruct):
        expand_group_by_all(sqlglot.parse_one(sql, read="bigquery"))
    for prove in PROVERS:
        result = prove(sql, sql, schema=SCHEMA)
        assert result.status is SmtStatus.NOT_PROVEN and "GROUP BY ALL" in result.reason

def test_group_by_all_in_derived_tables_and_ctes_is_spelled_out():
    tree = expand_group_by_all(sqlglot.parse_one(
        "WITH c AS (SELECT a, b, COUNT(*) AS n FROM t GROUP BY ALL) SELECT x.a FROM (SELECT a, MAX(n) AS m FROM c GROUP BY ALL) AS x",
        read="bigquery",
    ))
    assert sorted(g.sql() for g in tree.find_all(sqlglot.exp.Group)) == ["GROUP BY 1", "GROUP BY 1, 2"]

@pytest.mark.parametrize("prove", PROVERS)
def test_a_key_column_may_also_appear_next_to_an_aggregate(prove):
    # BigQuery accepts this (a is a key); another engine's binder does not, so it is not executed here.
    left, right = "SELECT a, COUNT(*) + a AS n FROM t GROUP BY ALL", "SELECT a, COUNT(*) + a AS n FROM t GROUP BY a"
    assert prove(left, right, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT
