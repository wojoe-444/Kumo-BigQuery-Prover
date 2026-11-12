"""False proofs from the ORDER BY / LIMIT / OFFSET tail of a parenthesized set operation (issue #537, S027-138..142).

In sqlglot the tail of ``(a UNION b) ORDER BY k LIMIT 1`` hangs on the enclosing ``Subquery``. Code that unwrapped
the parentheses to reach the set operation read the tail off the wrong node and dropped it, so both provers called
the query equivalent to the same set operation with no tail. Every pair below returns different rows (another engine, optimizer
off, shows it on the database in ``ROWS``), so neither prover may call it equivalent; the near misses that are
equivalent stay proven, so the fix is not just a wider refusal.
"""

from collections import Counter

import pytest

import sqlglot

pytest.importorskip("z3")

from sqlglot_support import skip_if_unparseable

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.ast_utils import merge_wrapper_tails

from kumo_bq_prover.ported.smt_equivalence import prove_equivalent_smt

SCHEMA = {"t": {"k": "INT64"}, "u": {"k": "INT64"}}

ROWS = {"t": [(3,), (3,), (3,), (4,), (None,)], "u": [(3,), (4,), (None,)]}

UNION = "(SELECT k FROM t UNION DISTINCT SELECT k FROM t)"

PLAIN = "SELECT k FROM t UNION DISTINCT SELECT k FROM t"

def _verdicts(left: str, right: str) -> list[bool]:
    return [prove(left, right, schema=SCHEMA, dialect="bigquery", timeout_ms=1500).proven for prove in (prove_equivalent_algebraic, prove_equivalent_smt)]

WRONG_PROOFS = [
    pytest.param(f"{UNION} ORDER BY k NULLS LAST LIMIT 1 OFFSET 1", PLAIN, id="138-top-level"),
    pytest.param(
        f"SELECT k FROM ({UNION} ORDER BY k NULLS LAST LIMIT 1) AS d",
        f"SELECT k FROM ({PLAIN}) AS d",
        id="139-derived-table",
    ),
    pytest.param(
        f"WITH c AS ({UNION} ORDER BY k NULLS LAST LIMIT 1 OFFSET 5) SELECT k FROM c",
        f"WITH c AS ({PLAIN}) SELECT k FROM c",
        id="140-cte",
    ),
    pytest.param(
        f"SELECT k FROM ({UNION} ORDER BY k NULLS LAST LIMIT 1 OFFSET 1) AS d WHERE k > 0",
        f"SELECT k FROM ({PLAIN}) AS d WHERE k > 0",
        id="141-outer-where",
    ),
    pytest.param(
        f"WITH c AS ({UNION} ORDER BY k NULLS LAST LIMIT 1 OFFSET 5), e AS (SELECT k FROM c) SELECT k FROM e",
        f"WITH c AS ({PLAIN}), e AS (SELECT k FROM c) SELECT k FROM e",
        id="142-cte-then-nesting",
    ),
    # the same root in other places a parenthesized query can sit
    pytest.param(
        f"SELECT k FROM t AS o WHERE k IN ({UNION} ORDER BY k LIMIT 1)",
        f"SELECT k FROM t AS o WHERE k IN ({PLAIN})",
        id="in-subquery",
    ),
    pytest.param(
        f"SELECT k FROM t AS o WHERE EXISTS ({UNION} ORDER BY k LIMIT 0)",
        f"SELECT k FROM t AS o WHERE EXISTS ({PLAIN})",
        id="exists-subquery",
    ),
    pytest.param(
        f"SELECT d.k FROM ({UNION} ORDER BY k LIMIT 1) AS d JOIN t ON d.k = t.k",
        f"SELECT d.k FROM ({PLAIN}) AS d JOIN t ON d.k = t.k",
        id="join-source",
    ),
    pytest.param("(SELECT k FROM t) ORDER BY k LIMIT 1", "SELECT k FROM t", id="parenthesized-select"),
    pytest.param(f"{UNION} ORDER BY k LIMIT 1 OFFSET 1", f"{UNION} ORDER BY k LIMIT 1 OFFSET 2", id="different-offsets"),
]

STACKED = [
    # two layers cut rows: the second smallest of the three smallest
    pytest.param(
        "SELECT k FROM ((SELECT k FROM t ORDER BY k LIMIT 3) ORDER BY k LIMIT 1 OFFSET 1) AS d",
        "SELECT k FROM (SELECT k FROM t ORDER BY k LIMIT 1) AS d",
        "SELECT k FROM (SELECT k FROM (SELECT k FROM t ORDER BY k LIMIT 3) ORDER BY k LIMIT 1 OFFSET 1) AS d",
        id="two-cuts",
    ),
    # an ORDER BY outside the cut reorders the survivors, so the tails cannot be folded into one
    pytest.param(
        "SELECT k FROM ((SELECT k FROM t ORDER BY k LIMIT 3) ORDER BY k DESC LIMIT 1) AS d",
        "SELECT k FROM (SELECT k FROM t ORDER BY k LIMIT 1) AS d",
        "SELECT k FROM (SELECT k FROM (SELECT k FROM t ORDER BY k LIMIT 3) ORDER BY k DESC LIMIT 1) AS d",
        id="reorder-after-cut",
    ),
]

STILL_PROVEN = [
    pytest.param(f"{UNION} ORDER BY k LIMIT 2", f"{PLAIN} ORDER BY k LIMIT 2", [True, True], id="parentheses-or-not"),
    pytest.param("(SELECT k FROM t) ORDER BY k LIMIT 2", "SELECT k FROM t ORDER BY k LIMIT 2", [True, True], id="parenthesized-select"),
    pytest.param(
        f"WITH c AS ({UNION} ORDER BY k LIMIT 2) SELECT k FROM c",
        f"WITH c AS ({PLAIN} ORDER BY k LIMIT 2) SELECT k FROM c",
        [None, True],
        id="cte",
    ),
    pytest.param(
        f"SELECT k FROM ({UNION} ORDER BY k LIMIT 2) AS d",
        f"SELECT k FROM ({PLAIN} ORDER BY k LIMIT 2) AS d",
        [None, True],
        id="derived-table",
    ),
]

def _parse(sql: str):
    return sqlglot.parse_one(sql, read="bigquery")

def test_the_tail_moves_onto_the_set_operation():
    merged = merge_wrapper_tails(_parse(f"{UNION} ORDER BY k LIMIT 1 OFFSET 1"))
    assert isinstance(merged, sqlglot.exp.Union)
    assert merged.sql("bigquery") == f"{PLAIN} ORDER BY k LIMIT 1 OFFSET 1"

def test_a_with_clause_on_the_parentheses_comes_along():
    merged = merge_wrapper_tails(_parse(f"WITH c AS (SELECT k FROM u) (SELECT k FROM c UNION ALL SELECT k FROM c) ORDER BY k LIMIT 1"))
    assert merged.sql("bigquery") == "WITH c AS (SELECT k FROM u) SELECT k FROM c UNION ALL SELECT k FROM c ORDER BY k LIMIT 1"

def test_an_ordering_without_a_cut_is_left_alone():
    # an ORDER BY with no LIMIT or OFFSET cannot change the bag
    inner = merge_wrapper_tails(_parse(f"{UNION} ORDER BY k"))
    assert inner.args.get("limit") is None and inner.args.get("offset") is None

@pytest.mark.parametrize(
    "sql",
    ["((SELECT k FROM t LIMIT 3) LIMIT 1)", "((SELECT k FROM t ORDER BY k LIMIT 3) ORDER BY k DESC)"],
)
def test_layers_that_cannot_fold_into_one_tail_give_none(sql):
    assert merge_wrapper_tails(_parse(sql)) is None
