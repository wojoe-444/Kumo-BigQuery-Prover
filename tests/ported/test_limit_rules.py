from collections import Counter

import pytest

import sqlglot

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.limit_rules import limit_rule

SCHEMA = {"t": ["a", "b"], "u": ["a", "b"], "dept": ["deptno", "name"]}

def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="mysql", compare_names=False).proven

def _rule(sql):
    tree = sqlglot.parse_one(sql, read="mysql")
    out = limit_rule(tree)
    return out.sql(dialect="mysql") if out is not None else None

def test_top_k_over_union_all_equals_pre_limited_branches_when_the_order_covers_the_output():
    union = "SELECT a FROM t UNION ALL SELECT a FROM u"
    left = f"SELECT d.a FROM ({union}) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    right = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 9)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, right)
    # A branch cut shorter than LIMIT + OFFSET can lose rows the outer cut keeps.
    short = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 3) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, short)
    # A branch with its own OFFSET drops rows.
    skipped = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a LIMIT 4 OFFSET 1) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, skipped)
    # The other direction pushes the cut the wrong way.
    descending = "SELECT d.a FROM ((SELECT a FROM t ORDER BY a DESC LIMIT 4) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 4)) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert not _proven(left, descending)

def test_union_pushdown_needs_the_order_to_cover_every_output_column():
    # Ordered by a alone, a tie on a can keep either b: the branch cut and the outer cut may break it differently.
    left = "SELECT d.a, d.b FROM (SELECT a, b FROM t UNION ALL SELECT a, b FROM u) AS d ORDER BY d.a LIMIT 1"
    right = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a LIMIT 1)) AS d ORDER BY d.a LIMIT 1"
    assert not _proven(left, right)
    # Ordered by both columns, ties are equal rows.
    left_total = left.replace("ORDER BY d.a LIMIT", "ORDER BY d.a, d.b LIMIT")
    right_total = "SELECT d.a, d.b FROM ((SELECT a, b FROM t ORDER BY a, b LIMIT 1) UNION ALL (SELECT a, b FROM u ORDER BY a, b LIMIT 1)) AS d ORDER BY d.a, d.b LIMIT 1"
    assert _proven(left_total, right_total)

def test_nested_cuts_on_the_same_order_merge():
    left = "SELECT d.a FROM (SELECT a, b FROM t ORDER BY a LIMIT 10 OFFSET 2) AS d ORDER BY d.a LIMIT 3 OFFSET 1"
    assert _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 3")
    assert not _proven(left, "SELECT a FROM t ORDER BY a LIMIT 3 OFFSET 1")
    # The inner cut runs out first: rows 3..11 of t, then 2 skipped, so at most 8 survive.
    short = "SELECT d.a FROM (SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 2) AS d ORDER BY d.a LIMIT 20 OFFSET 1"
    assert _proven(short, "SELECT a FROM t ORDER BY a LIMIT 8 OFFSET 3")
    assert not _proven(short, "SELECT a FROM t ORDER BY a LIMIT 9 OFFSET 3")

def test_nested_cuts_do_not_merge_on_other_orders_or_hidden_ties():
    other = "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(other, "SELECT a FROM t ORDER BY a LIMIT 3")
    # The outer output b is not fixed by the key a, so which tied row each cut keeps matters.
    ties = "SELECT d.b FROM (SELECT a, b FROM t ORDER BY a LIMIT 10) AS d ORDER BY d.a LIMIT 3"
    assert not _proven(ties, "SELECT b FROM t ORDER BY a LIMIT 3")

def test_offset_without_limit_is_an_unbounded_cut():
    assert _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT x.a FROM t AS x ORDER BY x.a OFFSET 2")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a OFFSET 1")
    assert not _proven("SELECT a FROM t ORDER BY a OFFSET 2", "SELECT a FROM t ORDER BY a DESC OFFSET 2")
    # OFFSET without ORDER BY skips arbitrary rows.
    assert not _proven("SELECT a FROM t OFFSET 2", "SELECT a FROM t OFFSET 2")

def test_cut_lifts_out_of_nested_projections():
    left = "SELECT e.c FROM (SELECT d.a + 1 AS c FROM (SELECT a, b FROM t ORDER BY b OFFSET 1) AS d) AS e"
    assert _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 1")
    assert not _proven(left, "SELECT a + 1 AS c FROM t ORDER BY b OFFSET 2")
    # DISTINCT before the cut is not a projection of it.
    distinct = "SELECT d.a FROM (SELECT DISTINCT a, b FROM t ORDER BY a, b LIMIT 2) AS d"
    assert _rule(distinct) is None

def test_repeated_order_key_and_unread_order_are_dropped():
    assert _rule("SELECT a FROM t ORDER BY a, b DESC, a DESC LIMIT 2") == "SELECT a FROM t ORDER BY a, b DESC LIMIT 2"
    assert _proven("SELECT d.a FROM (SELECT a FROM t ORDER BY b) AS d", "SELECT a FROM t")

def test_order_read_by_a_cut_without_order_or_an_array_is_kept():
    # LIMIT without ORDER BY may follow the order of its input, so the inner ORDER BY stays.
    sql = "SELECT d.a FROM (SELECT a FROM t ORDER BY a) AS d LIMIT 1"
    tree = sqlglot.parse_one(sql, read="mysql")
    inner = tree.find(sqlglot.exp.Subquery).this
    assert limit_rule(inner) is None
    assert not _proven(sql, "SELECT d.a FROM (SELECT a FROM t ORDER BY a DESC) AS d LIMIT 1")
    array = sqlglot.parse_one("SELECT ARRAY(SELECT a FROM t ORDER BY a) AS x", read="bigquery")
    inner = [s for s in array.find_all(sqlglot.exp.Select) if s is not array]
    assert inner and all(limit_rule(s) is None for s in inner)

def test_union_branch_cut_reads_as_a_derived_table():
    branch = "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY a LIMIT 1)"
    assert _proven(f"({branch}) ORDER BY a", branch)
    assert not _proven(branch, "(SELECT a, b FROM t LIMIT 0) UNION ALL (SELECT a, b FROM t ORDER BY b LIMIT 1)")

def test_a_cut_to_no_rows_over_a_union_of_empty_cuts_is_an_empty_query():
    left = "SELECT d.a FROM (SELECT a FROM t UNION ALL SELECT a FROM u) AS d ORDER BY d.a LIMIT 0"
    right = "SELECT e.a FROM ((SELECT a FROM t ORDER BY a LIMIT 0) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 0)) AS e ORDER BY e.a LIMIT 0"
    assert _proven(left, right)
    assert _proven(right, "SELECT a FROM t LIMIT 0")
    # One branch that still returns rows is not empty.
    assert not _proven(right, "SELECT e.a FROM ((SELECT a FROM t ORDER BY a LIMIT 0) UNION ALL (SELECT a FROM u ORDER BY a LIMIT 1)) AS e")
    # A global aggregate over an emptied source still returns its one row.
    count = "SELECT COUNT(*) AS n FROM (SELECT a FROM t ORDER BY a LIMIT 1) AS d WHERE FALSE"
    assert not _proven(count, "SELECT a FROM t LIMIT 0")
    assert _proven(count, "SELECT 0 AS n")

def _proven_bigquery(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery").proven

LOST_GLOBAL_AGGREGATE = [
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t",
        [(1, 1), (2, 1), (3, 1)],
        id="s007-001-cut-over-a-global-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t",
        [],
        id="s007-001-cut-over-a-global-aggregate-of-nothing",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) d",
        "SELECT 7 AS c FROM t ORDER BY TRUE LIMIT 2",
        [(1, 1), (2, 1), (3, 1)],
        id="s007-001-lifted-cut",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT 7 AS c, COUNT(*) AS n FROM t) d",
        "SELECT 7 AS c FROM t",
        [(1, 1), (2, 1)],
        id="s007-001-pruned-derived-aggregate",
    ),
    pytest.param(
        "SELECT 1 AS one FROM (SELECT MAX(a) AS n FROM t) d",
        "SELECT 1 AS one FROM t",
        [],
        id="s007-001-folded-derived-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 'a' AS c FROM t) d",
        "SELECT 'a' AS c FROM t",
        [(1, 1), (2, 1)],
        id="s007-001-unwrapped-derived-aggregate",
    ),
    pytest.param(
        "SELECT d.c FROM (SELECT COUNT(*) AS n, 'a' AS c FROM t ORDER BY 1 LIMIT 2) d",
        "SELECT 'a' AS c FROM t",
        [],
        id="s007-001-unwrapped-cut-aggregate",
    ),
]

def test_s007_001_cut_does_not_lift_off_the_last_aggregate():
    assert _rule("SELECT d.c FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) AS d") is None
    # The aggregate read through the cut still lifts, and a grouped select keeps its groups without one.
    assert _rule("SELECT d.n FROM (SELECT COUNT(*) AS n, 7 AS c FROM t ORDER BY TRUE LIMIT 2) AS d") == "SELECT COUNT(*) AS n FROM t ORDER BY TRUE LIMIT 2"
    assert _rule("SELECT d.a FROM (SELECT a, COUNT(*) AS n FROM t GROUP BY a ORDER BY a LIMIT 2) AS d") == "SELECT a FROM t GROUP BY a ORDER BY a LIMIT 2"

REBOUND_GROUPING_NAMES = [
    pytest.param(
        "SELECT d.b + 0 AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b) d",
        "SELECT b + 0 AS bb FROM t GROUP BY b",
        [(1, 1), (2, 1)],
        id="s007-001-folded-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b, d.b + 0 AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b ORDER BY b LIMIT 3) d",
        "SELECT b, b + 0 AS bb FROM t GROUP BY b ORDER BY b LIMIT 3",
        [(1, 1), (2, 1)],
        id="s007-001-lifted-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b AS bb FROM (SELECT a AS bb, b FROM t GROUP BY bb, b ORDER BY b LIMIT 5) d",
        "SELECT b AS bb FROM t GROUP BY b ORDER BY b LIMIT 5",
        [(1, 1), (2, 1)],
        id="s007-001-root-lifted-alias-group-key",
    ),
    pytest.param(
        "SELECT d.b AS a FROM (SELECT b, COUNT(*) AS n FROM t GROUP BY b HAVING MAX(a) > 1) d",
        "SELECT b AS a FROM t GROUP BY b HAVING MAX(b) > 1",
        [(1, 1), (2, 1), (1, 2)],
        id="s007-001-unwrapped-alias-captures-having",
    ),
    pytest.param(
        "SELECT d.b AS a FROM (SELECT b, MAX(a) AS m FROM t GROUP BY b) d WHERE d.m > 1",
        "SELECT b AS a FROM t GROUP BY b HAVING MAX(b) > 1",
        [(1, 1), (2, 1), (1, 2)],
        id="s007-001-folded-alias-captures-filter",
    ),
]

UNCAST_LEFT = "SELECT x FROM t ORDER BY CAST(x AS DOUBLE), x DESC LIMIT 1"

UNCAST_RIGHT = "SELECT x FROM t ORDER BY x, x DESC LIMIT 1"

def _uncast(dialect, declared):
    tree = sqlglot.parse_one("SELECT x FROM t ORDER BY CAST(x AS DOUBLE) LIMIT 1", read=dialect)
    out = limit_rule(tree, {"t": {"x": declared}}, dialect)
    return out.sql(dialect=dialect) if out is not None else None

def _uncast_proven(dialect):
    return prove_equivalent_algebraic(UNCAST_LEFT, UNCAST_RIGHT, schema={"t": ["x"]}, types={"t": {"x": "int"}}, dialect=dialect).proven

def test_s007_001_uncast_snowflake_int_is_not_proven():
    assert not _uncast_proven("snowflake")

def test_s007_001_uncast_near_miss_postgres_int_stays_proven():
    assert _uncast("postgres", "int") == "SELECT x FROM t ORDER BY x LIMIT 1"
    assert _uncast("postgres", "bigint") is None
    assert _uncast_proven("postgres")

NESTED_NAME_CAPTURE = [
    pytest.param(
        "SELECT d.b + (SELECT MAX(b) FROM u) AS m FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d",
        "SELECT a + (SELECT MAX(a) FROM u) AS m FROM t ORDER BY a LIMIT 1",
        id="lifted-cut-subquery-reads-its-own-column",
    ),
    pytest.param(
        "SELECT d.b + (SELECT MAX(b) FROM u WHERE b > 1) AS m FROM (SELECT a AS b FROM t ORDER BY a LIMIT 1) AS d",
        "SELECT a + (SELECT MAX(a) FROM u WHERE a > 1) AS m FROM t ORDER BY a LIMIT 1",
        id="lifted-cut-subquery-filter-reads-its-own-column",
    ),
]

def test_a_lifted_cut_does_not_draw_a_random_value_twice():
    left = "SELECT d.r, d.r AS r2 FROM (SELECT RAND() AS r FROM t ORDER BY a LIMIT 1) AS d"
    assert limit_rule(sqlglot.parse_one(left, read="bigquery")) is None
    assert not _proven_bigquery(left, "SELECT RAND() AS r, RAND() AS r2 FROM t ORDER BY a LIMIT 1")

@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT d.c FROM (SELECT a, COUNT(*) AS c FROM t GROUP BY 1 ORDER BY 2, 1 LIMIT 3) AS d", id="group-by-ordinal-moves"),
        pytest.param("SELECT d.a FROM (SELECT COUNT(*) AS c, a FROM t GROUP BY 2 ORDER BY 1 LIMIT 3) AS d", id="group-by-second-ordinal-moves"),
    ],
)
def test_a_lifted_cut_does_not_move_the_item_a_group_by_ordinal_names(sql):
    assert limit_rule(sqlglot.parse_one(sql, read="bigquery")) is None
