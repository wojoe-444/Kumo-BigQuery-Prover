"""A global aggregate returns at most one row, so its ORDER BY and LIMIT n >= 1 are dropped (``kumo_bq_prover.ported.one_row_tails``)."""

import random

import pytest

import sqlglot

from sqlglot import exp

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.one_row_tails import drop_one_row_tail

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}

TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}

REPORT = "SELECT COUNT(DISTINCT t.a) AS n, SUM(t.b) AS s FROM t WHERE t.c = 1"

def dropped(sql, dialect="postgres"):
    out = drop_one_row_tail(sqlglot.parse_one(sql, read=dialect))
    return out.sql(dialect=dialect) if out is not None else None

def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False).proven

def test_the_tail_of_a_global_aggregate_is_dropped():
    assert dropped(REPORT + " ORDER BY COUNT(DISTINCT t.a) LIMIT 100") == REPORT
    assert dropped(REPORT + " ORDER BY 1 DESC NULLS LAST, s") == REPORT
    assert dropped(REPORT + " LIMIT 1") == REPORT
    assert dropped("SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 2 ORDER BY m LIMIT 5") == "SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 2"
    assert dropped("SELECT 7 AS seven FROM t HAVING COUNT(*) > 2 ORDER BY 1 LIMIT 5") == "SELECT 7 AS seven FROM t HAVING COUNT(*) > 2"
    assert dropped("SELECT DISTINCT SUM(t.a) + 1 AS m FROM t JOIN u ON t.a = u.a ORDER BY 1 LIMIT 3") == "SELECT DISTINCT SUM(t.a) + 1 AS m FROM t JOIN u ON t.a = u.a"

@pytest.mark.parametrize("sql", [
    REPORT,  # no tail
    "SELECT t.a, COUNT(*) AS n FROM t GROUP BY t.a ORDER BY n LIMIT 1",  # a row per group
    "SELECT COUNT(*) AS n FROM t GROUP BY () ORDER BY n LIMIT 1",
    "SELECT t.a FROM t ORDER BY t.a LIMIT 1",  # no aggregate
    "SELECT 1 AS one FROM t ORDER BY COUNT(*) LIMIT 1",  # an aggregate by its ORDER BY alone
    REPORT + " LIMIT 0",
    REPORT + " ORDER BY 1 LIMIT 5 OFFSET 1",
    REPORT + " OFFSET 1",
    REPORT + " ORDER BY 1 LIMIT 2 + 3",
    REPORT + " ORDER BY 1 LIMIT $1",
    REPORT + " ORDER BY 1 FETCH FIRST 5 ROWS ONLY",
    "SELECT SUM(t.a) AS s, ROW_NUMBER() OVER (ORDER BY SUM(t.a)) AS r FROM t ORDER BY 1 LIMIT 1",
    "SELECT SUM(t.a) OVER () AS s FROM t ORDER BY 1 LIMIT 1",  # a window, not an aggregate: a row per row of t
    "SELECT COUNT(*) AS n, UNNEST(ARRAY[1, 2, 3]) AS e FROM t ORDER BY e DESC LIMIT 1",  # three rows in PostgreSQL
    "SELECT COUNT(*) AS n, GENERATE_SERIES(1, 3) AS e FROM t ORDER BY e DESC LIMIT 1",
    "SELECT COUNT(*) AS n, my_rows(3) AS e FROM t ORDER BY e DESC LIMIT 1",
    "SELECT DISTINCT ON (1) COUNT(*) AS n FROM t ORDER BY 1 LIMIT 1",
])
def test_other_shapes_keep_their_tail(sql):
    assert dropped(sql) is None

def test_a_select_inside_another_keeps_its_tail():
    # SUM(t.b) aggregates the enclosing query's rows there: the subquery returns a row per row of u
    nested = sqlglot.parse_one("SELECT t.a, (SELECT SUM(t.b) FROM u ORDER BY u.c LIMIT 1) AS s FROM t GROUP BY t.a", read="postgres")
    inner = nested.find(exp.Subquery).this
    assert drop_one_row_tail(inner) is None
    derived = sqlglot.parse_one("SELECT d.n FROM (SELECT COUNT(*) AS n FROM t ORDER BY 1 LIMIT 5) AS d", read="postgres")
    assert drop_one_row_tail(derived.find(exp.Subquery).this) is None

TARGETS = [
    (REPORT + " ORDER BY COUNT(DISTINCT t.a) LIMIT 100", REPORT),
    (REPORT + " ORDER BY COUNT(DISTINCT t.a) LIMIT 100", REPORT + " LIMIT 100"),
    (REPORT + " ORDER BY COUNT(DISTINCT t.a) LIMIT 100", REPORT + " ORDER BY 2 DESC"),
    (REPORT + " LIMIT 1", "SELECT COUNT(DISTINCT x.a) AS n, SUM(x.b) AS s FROM (SELECT * FROM t WHERE c = 1) AS x ORDER BY n"),
    ("SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 2 ORDER BY m LIMIT 5", "SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 2"),
]

@pytest.mark.parametrize("left, right", TARGETS)
def test_one_row_reports_prove(left, right):
    assert proved(left, right)

ROWS = {"t": [(1, 5, 1), (2, 7, 1), (2, 1, 1), (3, 9, 2), (None, 4, 1)], "u": [(1, 1, 1), (2, 2, 2)]}

NEAR_MISSES = [
    # LIMIT 0 and OFFSET 1 drop the one row
    (REPORT + " ORDER BY 1 LIMIT 100", REPORT + " LIMIT 0"),
    (REPORT + " ORDER BY 1 LIMIT 100", REPORT + " ORDER BY 1 LIMIT 100 OFFSET 1"),
    # with a GROUP BY the tail chooses among the groups
    ("SELECT COUNT(*) AS n FROM t GROUP BY t.c ORDER BY n DESC LIMIT 1", "SELECT COUNT(*) AS n FROM t GROUP BY t.c"),
    ("SELECT COUNT(*) AS n FROM t GROUP BY t.c ORDER BY n DESC LIMIT 1", "SELECT COUNT(*) AS n FROM t"),
    # another filter, another aggregate
    (REPORT + " ORDER BY 1 LIMIT 100", REPORT.replace("t.c = 1", "t.c = 2")),
    (REPORT + " ORDER BY 1 LIMIT 100", REPORT.replace("COUNT(DISTINCT t.a)", "COUNT(t.a)")),
    # HAVING can remove the row: not the same as no HAVING
    ("SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 9 ORDER BY m LIMIT 5", "SELECT MAX(t.a) AS m FROM t"),
]

DROPPED_QUERIES = [
    REPORT + " ORDER BY COUNT(DISTINCT t.a) LIMIT 100",
    REPORT + " LIMIT 1",
    "SELECT MAX(t.a) AS m, MIN(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a ORDER BY 2 DESC, 1 LIMIT 3",
    "SELECT MAX(t.a) AS m FROM t HAVING COUNT(*) > 2 ORDER BY m LIMIT 5",
    "SELECT DISTINCT SUM(t.a) + 1 AS m FROM t JOIN u ON t.a = u.a ORDER BY 1 LIMIT 3",
]

def random_rows(rng):
    def table(size):
        rows = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 1, 2]), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(6), "u": table(4)}
