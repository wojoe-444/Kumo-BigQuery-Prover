"""Two EXISTS tests over the same FROM joined by OR read as one (``exists_disjunction``)."""

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

SCHEMA = {"t": ["a", "b"], "u": ["a", "c"], "v": ["a", "c"]}

def proven(left, right, dialect="bigquery"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect=dialect, compare_names=False).proven

PROVED = [
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) OR EXISTS (SELECT 1 FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2)",
    ),
    # correlated, with another condition around it and different select lists
    (
        "SELECT a FROM t WHERE b < 10 AND (EXISTS (SELECT u.a FROM u WHERE u.a = t.a AND u.c > 5) OR EXISTS (SELECT 1 FROM u WHERE u.a = t.b))",
        "SELECT a FROM t WHERE b < 10 AND EXISTS (SELECT 1 FROM u WHERE (u.a = t.a AND u.c > 5) OR u.a = t.b)",
    ),
    # three members, and one without a filter
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE c = 1) OR EXISTS (SELECT 1 FROM u WHERE c = 2) OR EXISTS (SELECT 1 FROM u WHERE c = 3)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE c IN (1, 2, 3))",
    ),
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE c = 1) OR EXISTS (SELECT 1 FROM u)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u)",
    ),
    # De Morgan
    (
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a AND u.c = 1) AND NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a AND u.c = 2)",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.a = t.a AND (u.c = 1 OR u.c = 2))",
    ),
]

@pytest.mark.parametrize("left, right", PROVED)
@pytest.mark.parametrize("dialect", ["bigquery", "postgres"])
def test_existence_tests_over_one_source_merge(left, right, dialect):
    assert proven(left, right, dialect)

NOT_PROVED = [
    # AND is not OR: one row passing p and another passing q is not a row passing both
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) AND EXISTS (SELECT 1 FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 AND u.c = 2)",
    ),
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) AND EXISTS (SELECT 1 FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2)",
    ),
    # different sources
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) OR EXISTS (SELECT 1 FROM v WHERE v.c = 2)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2)",
    ),
    # NOT EXISTS joined by OR
    (
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.c = 1) OR NOT EXISTS (SELECT 1 FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2)",
    ),
    # a mix of EXISTS and NOT EXISTS
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) OR NOT EXISTS (SELECT 1 FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2)",
    ),
    # a global aggregate returns a row whatever its filter keeps
    (
        "SELECT a FROM t WHERE EXISTS (SELECT COUNT(*) FROM u WHERE u.c = 1) OR EXISTS (SELECT COUNT(*) FROM u WHERE u.c = 2)",
        "SELECT a FROM t WHERE FALSE",
    ),
    # a HAVING decides more than the filter does
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 GROUP BY u.a HAVING COUNT(*) > 1) OR EXISTS (SELECT 1 FROM u WHERE u.c = 2 GROUP BY u.a HAVING COUNT(*) > 1)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1 OR u.c = 2 GROUP BY u.a HAVING COUNT(*) > 1)",
    ),
    # a NULL filter keeps no row
    (
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.c = 1) OR EXISTS (SELECT 1 FROM u WHERE u.c <> 1)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u)",
    ),
]

@pytest.mark.parametrize("left, right", NOT_PROVED)
def test_near_misses_are_not_proved(left, right):
    assert not proven(left, right)
    assert not proven(right, left)
