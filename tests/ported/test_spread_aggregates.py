"""``STDDEV`` and ``VARIANCE`` as a function of three sums (``kumo_bq_prover.ported.spread_aggregates``)."""

import math

import random

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported import shape_fallback

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.spread_aggregates import ASSUMPTION, rewrite_spread_aggregates

SCHEMA = {"t": ["k", "x", "y"], "u": ["k", "z"]}

TYPES = {"t": {"k": "BIGINT", "x": "BIGINT", "y": "BIGINT"}, "u": {"k": "BIGINT", "z": "BIGINT"}}

def rewritten(sql, dialect="postgres"):
    return rewrite_spread_aggregates(sqlglot.parse_one(sql, read=dialect), dialect).sql(dialect=dialect)

def prove(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False, dialect=dialect)

def proved(left, right, dialect="postgres"):
    return prove(left, right, dialect).proven

def test_the_call_becomes_a_function_of_three_sums():
    assert rewritten("SELECT k, STDDEV_SAMP(x) FROM t GROUP BY k") == "SELECT k, KUMOSQL_STDDEV_SAMP(SUM((x) * (x)), SUM(x), COUNT(x)) FROM t GROUP BY k"
    assert rewritten("SELECT STDDEV(x + y) FROM t") == "SELECT KUMOSQL_STDDEV_SAMP(SUM((x + y) * (x + y)), SUM(x + y), COUNT(x + y)) FROM t"
    assert "KUMOSQL_STDDEV_POP(" in rewritten("SELECT STDDEV_POP(x) FROM t")
    assert "KUMOSQL_VAR_SAMP(" in rewritten("SELECT VARIANCE(x) FROM t")
    assert "KUMOSQL_VAR_SAMP(" in rewritten("SELECT VAR_SAMP(x) FROM t")
    assert "KUMOSQL_VAR_POP(" in rewritten("SELECT VAR_POP(x) FROM t")
    assert "KUMOSQL_STDDEV_SAMP(" in rewritten("SELECT k FROM t GROUP BY k HAVING STDDEV_SAMP(x) > 1 ORDER BY STDDEV_SAMP(y)")
    assert "KUMOSQL_STDDEV_SAMP(" in rewritten("SELECT STDDEV(x) FROM t", "bigquery")

def test_what_is_left_alone():
    for sql, dialect in (
        ("SELECT STDDEV_SAMP(x) OVER (PARTITION BY k) FROM t", "postgres"),
        ("SELECT STDDEV_SAMP(x) FILTER(WHERE y > 0) FROM t", "postgres"),
        ("SELECT STDDEV_SAMP(DISTINCT x) FROM t", "postgres"),  # 2 and -2: two values, one square
        ("SELECT STDDEV_SAMP((SELECT MAX(z) FROM u)) FROM t", "postgres"),
        ("SELECT STDDEV_SAMP(ABS(x)) FROM t", "postgres"),
        ("SELECT STDDEV_SAMP(x * RANDOM()) FROM t", "postgres"),
        ("SELECT STDDEV(x) FROM t", "mysql"),  # the population form there
        ("SELECT VARIANCE(x) FROM t", "mysql"),
        ("SELECT AVG(x), SUM(x), MAX(x) FROM t", "postgres"),
    ):
        assert rewritten(sql, dialect) == sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect), sql

def test_pairs_it_proves():
    comma = "SELECT t.k, STDDEV_SAMP(t.x) AS s, AVG(t.x) AS m FROM t, u WHERE t.k = u.k AND u.z > 3 GROUP BY t.k"
    joined = "SELECT t.k, STDDEV_SAMP(t.x) AS s, AVG(t.x) AS m FROM t JOIN u ON t.k = u.k WHERE u.z > 3 GROUP BY t.k"
    result = prove(comma, joined)
    assert result.proven and ASSUMPTION in result.assumptions
    # the grouped values read through a derived table, with the ratio the DSB inventory queries compute
    assert proved(
        f"SELECT k, CASE m WHEN 0 THEN NULL ELSE s / m END FROM ({comma}) AS g WHERE CASE m WHEN 0 THEN 0 ELSE s / m END > 1",
        f"SELECT k, CASE WHEN m = 0 THEN NULL ELSE s / m END FROM ({joined}) AS g WHERE CASE WHEN m = 0 THEN 0 ELSE s / m END > 1",
    )
    assert proved("SELECT k FROM t GROUP BY k HAVING STDDEV(x) > 2", "SELECT k FROM t GROUP BY k HAVING STDDEV_SAMP(x) > 2")
    assert proved("SELECT VARIANCE(x) FROM t WHERE y = 1", "SELECT VAR_SAMP(g.x) FROM (SELECT x FROM t WHERE y = 1) AS g")

def test_near_misses_stay_unproven():
    base = "SELECT k, {} FROM t {} GROUP BY k"
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_POP(x)", ""))
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("VAR_SAMP(x)", ""))
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_SAMP(y)", ""))
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_SAMP(x + 1)", ""))  # true, but nothing is known of the function
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_SAMP(x)", "WHERE y > 0"))
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_SAMP(COALESCE(x, 0))", ""))  # a NULL is skipped, a 0 is not
    assert not proved(base.format("STDDEV_SAMP(x)", ""), base.format("STDDEV_SAMP(DISTINCT x)", ""))
    # a join that repeats rows changes the deviation
    assert not proved("SELECT t.k, STDDEV_SAMP(t.x) FROM t GROUP BY t.k", "SELECT t.k, STDDEV_SAMP(t.x) FROM t JOIN u ON t.k = u.k GROUP BY t.k")
    # the deviation of one group is not the deviation of all rows
    assert not proved("SELECT STDDEV_SAMP(x) FROM t", "SELECT STDDEV_SAMP(x) FROM t GROUP BY k")
    assert not proved("SELECT STDDEV_SAMP(x) FROM t", "SELECT 0 FROM t")
    # nothing is known about when the function is NULL (it is, for a group of fewer than two values)
    grouped = "SELECT k FROM t GROUP BY k"
    assert not proved(f"{grouped} HAVING STDDEV_SAMP(x) IS NOT NULL", grouped)
    assert not proved(f"{grouped} HAVING STDDEV_SAMP(x) IS NULL", "SELECT k FROM t WHERE 1 = 0 GROUP BY k")
    assert not proved(f"{grouped} HAVING STDDEV_SAMP(x) >= 0", grouped)
    assert not proved(f"{grouped} HAVING STDDEV_SAMP(x) = STDDEV_SAMP(x)", grouped)
    assert not proved("SELECT k, COALESCE(STDDEV_SAMP(x), 0) FROM t GROUP BY k", "SELECT k, STDDEV_SAMP(x) FROM t GROUP BY k")

def test_off_on_the_second_attempt():
    with shape_fallback.disabled():
        from kumo_bq_prover.ported.algebraic_equivalence import normalize

        assert "KUMOSQL_" not in normalize("SELECT k, STDDEV_SAMP(x) FROM t GROUP BY k", schema=SCHEMA, dialect="postgres", types=TYPES)
