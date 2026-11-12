from collections import Counter

import pytest

import sqlglot

from kumo_bq_prover.ported.nonnull_any import rewrite_nonnull_any

SCHEMA = {"a": ["x", "g"], "b": ["y", "g"]}

NN = {"a": {"x"}, "b": {"y"}}

TYPES = {"a": {"x": "INT", "g": "VARCHAR"}, "b": {"y": "INT", "g": "VARCHAR"}}

LEFT = "SELECT a.x,a.x>ANY(SELECT b.y FROM b WHERE b.g=a.g) AS f FROM a"

RIGHT = "SELECT a.x,(a.x>q.m) IS TRUE AND q.c IS NOT NULL AS f FROM a LEFT JOIN (SELECT g,MIN(y) m,COUNT(*) c FROM b GROUP BY g) q ON a.g=q.g"

EXPANDED = """CASE WHEN EXISTS(SELECT 1 FROM b WHERE a.x>b.y)
THEN TRUE WHEN EXISTS(SELECT 1 FROM b WHERE a.x>b.y OR a.x IS NULL OR b.y IS NULL)
THEN NULL ELSE FALSE END"""

@pytest.mark.parametrize("query", [
    "SELECT COUNT(*),a.x>ANY(SELECT b.y FROM b) FROM a",
    "SELECT COUNT(*),(SELECT a.x>ANY(SELECT b.y FROM b) FROM a LIMIT 1) FROM b",
    f"SELECT COUNT(*),{EXPANDED} FROM a",
])
def test_nonnull_any_keeps_aggregate_output_phase(query):
    tree = sqlglot.parse_one(query)
    before = tree.sql()
    assert rewrite_nonnull_any(tree, SCHEMA, NN, TYPES, set()).sql() == before

FIRST = "EXISTS(SELECT 1 FROM b WHERE a.g=b.g AND a.x>b.y)"

SECOND = "EXISTS(SELECT 1 FROM b WHERE a.g=b.g AND (a.x IS NULL OR b.y IS NULL OR a.x>b.y))"

CORRELATED = f"CASE WHEN {FIRST} THEN TRUE WHEN {SECOND} THEN NULL ELSE FALSE END"
