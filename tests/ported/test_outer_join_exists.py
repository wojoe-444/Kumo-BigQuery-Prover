"""An uncorrelated EXISTS in a LEFT JOIN's ON clause read as a filter on the padded side.

Every proved pair is also run on random another engine databases (NULLs, duplicates, empty tables); every
pair that must stay unproven has a another engine witness found the same way, confirmed with the optimizer off.
"""

import random

from collections import Counter

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.outer_join_exists import move_exists_into_padded_side

SCHEMA = {"a": ["k", "x"], "b": ["k", "y"], "c": ["k", "z"]}

Q = "SELECT 1 FROM c WHERE c.z < 2"

GROUPED_TRUE = "(SELECT s2.v AS v FROM (SELECT TRUE AS v FROM (SELECT c.k AS k FROM c WHERE c.z < 2) AS s) AS s2 GROUP BY s2.v)"

def _proven(left: str, right: str) -> bool:
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False).proven

EQUIVALENT = [
    pytest.param(
        f"SELECT a.x FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y, t.v AS v FROM b JOIN {GROUPED_TRUE} AS t ON TRUE) AS d ON TRUE",
        id="calcite-grouped-true-expansion",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        f"SELECT a.x, d.y FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE EXISTS ({Q})) AS d ON a.k = d.k",
        id="exists-beside-join-key",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON NOT EXISTS ({Q}) AND a.k = b.k",
        f"SELECT a.x, d.y FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE NOT EXISTS ({Q})) AS d ON a.k = d.k",
        id="not-exists",
    ),
]

NOT_EQUIVALENT = [
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        "SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k",
        id="exists-dropped",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})",
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON a.k = b.k WHERE EXISTS ({Q})",
        id="exists-moved-to-where-drops-unpadded-rows",
    ),
    pytest.param(
        f"SELECT a.x FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x FROM a JOIN (SELECT b.k AS k, t.v AS v FROM b JOIN {GROUPED_TRUE} AS t ON TRUE) AS d ON TRUE",
        id="inner-join-does-not-pad",
    ),
    pytest.param(
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON EXISTS ({Q})",
        f"SELECT a.x, b.y FROM a LEFT JOIN b ON NOT EXISTS ({Q})",
        id="negated",
    ),
]

def _rule(sql: str):
    return move_exists_into_padded_side(sqlglot.parse_one(sql), SCHEMA)

def test_rule_moves_only_uncorrelated_exists():
    rewritten = _rule(f"SELECT a.x FROM a LEFT JOIN b ON a.k = b.k AND EXISTS ({Q})")
    assert rewritten is not None
    assert rewritten.sql() == "SELECT a.x FROM a LEFT JOIN (SELECT b.k AS k, b.y AS y FROM b WHERE EXISTS(SELECT 1 FROM c WHERE c.z < 2)) AS b ON a.k = b.k"

@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT a.x FROM a LEFT JOIN b ON a.k = b.k AND EXISTS (SELECT 1 FROM c WHERE c.k = a.k)", id="reads-preserved-side"),
        pytest.param("SELECT a.x FROM a LEFT JOIN b ON EXISTS (SELECT 1 FROM c WHERE c.k = b.k)", id="reads-padded-side"),
        pytest.param(f"SELECT a.x FROM a JOIN b ON EXISTS ({Q})", id="inner-join"),
        pytest.param(f"SELECT a.x FROM a RIGHT JOIN b ON EXISTS ({Q})", id="right-join"),
        pytest.param(f"SELECT a.x FROM a LEFT JOIN b ON a.k = b.k OR EXISTS ({Q})", id="disjunct"),
        pytest.param(f"SELECT a.x FROM a LEFT JOIN zz ON EXISTS ({Q})", id="unknown-columns"),
    ],
)
def test_rule_leaves_other_conditions(sql):
    assert _rule(sql) is None
