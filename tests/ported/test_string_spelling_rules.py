"""Identity string casts and ``LEFT`` read as ``SUBSTRING`` (``kumo_bq_prover.ported.string_spelling_rules``)."""

import random

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.string_spelling_rules import string_spelling_rules

TYPES = {
    "t": {"a": "INT", "s": "CHAR(2)", "n": "CHAR(9)", "v": "VARCHAR(20)", "w": "TEXT"},
    "u": {"a": "INT", "s": "CHAR(2)", "v": "VARCHAR(20)"},
}

SCHEMA = {name: list(columns) for name, columns in TYPES.items()}

DUCK = {"t": "a BIGINT, s VARCHAR, n VARCHAR, v VARCHAR, w VARCHAR", "u": "a BIGINT, s VARCHAR, v VARCHAR"}

def rewritten(sql, dialect="postgres", types=TYPES):
    out = string_spelling_rules(sqlglot.parse_one(sql, read=dialect), types, dialect)
    return out.sql(dialect=dialect) if out is not None else None

def proved(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven

def test_a_cast_to_the_declared_string_type_is_the_column():
    assert rewritten("SELECT a FROM t WHERE CAST(s AS CHAR(2)) IN ('GA', 'IL')") == "SELECT a FROM t WHERE s IN ('GA', 'IL')"
    assert rewritten("SELECT CAST(t.n AS CHAR(9)) AS n FROM t") == "SELECT t.n AS n FROM t"
    assert rewritten("SELECT a FROM t WHERE CAST(v AS VARCHAR(20)) = 'x'") == "SELECT a FROM t WHERE v = 'x'"
    assert rewritten("SELECT a FROM t WHERE CAST(v AS VARCHAR(64)) = 'x'") == "SELECT a FROM t WHERE v = 'x'"  # wider: nothing is cut
    # through a derived table that passes the column on, renamed
    assert (
        rewritten("SELECT d.a FROM (SELECT k.a AS a, k.s AS s0 FROM t AS k) AS d WHERE CAST(d.s0 AS CHAR(2)) = 'GA'")
        == "SELECT d.a FROM (SELECT k.a AS a, k.s AS s0 FROM t AS k) AS d WHERE d.s0 = 'GA'"
    )
    # in a join condition, with each side's own declared type
    assert (
        rewritten("SELECT t.a FROM t JOIN u ON CAST(t.s AS CHAR(2)) = CAST(u.s AS CHAR(2))")
        == "SELECT t.a FROM t JOIN u ON t.s = u.s"
    )

@pytest.mark.parametrize("sql", [
    "SELECT a FROM t WHERE CAST(n AS CHAR(6)) = 'Sunday'",  # shorter CHAR: cut to six characters
    "SELECT a FROM t WHERE CAST(s AS CHAR(5)) = 'GA'",  # longer CHAR: padded
    "SELECT a FROM t WHERE CAST(v AS VARCHAR(5)) = 'x'",  # shorter VARCHAR: cut
    "SELECT a FROM t WHERE CAST(s AS VARCHAR(2)) = 'GA'",  # CHAR to VARCHAR drops trailing blanks
    "SELECT a FROM t WHERE CAST(v AS CHAR(20)) = 'x'",  # VARCHAR to CHAR pads
    "SELECT a FROM t WHERE CAST(w AS VARCHAR(20)) = 'x'",  # TEXT has no length: the cast can cut
    "SELECT a FROM t WHERE CAST(a AS VARCHAR(20)) = '1'",  # not a string column
    "SELECT a FROM t WHERE CAST(UPPER(s) AS CHAR(2)) = 'GA'",  # an expression: its type is not declared
    "SELECT a FROM t WHERE CAST('GA' AS CHAR(2)) = s",  # a literal
    "SELECT a FROM t WHERE TRY_CAST(s AS CHAR(2)) = 'GA'",
    "SELECT a FROM t WHERE CAST(s AS CHAR) = 'G'",  # CHAR is CHAR(1) in PostgreSQL
    "SELECT t.a FROM t, u WHERE CAST(s AS CHAR(2)) = 'GA'",  # which table's s is not said
    "SELECT a FROM x WHERE CAST(s AS CHAR(2)) = 'GA'",  # a table with no declared types
    "SELECT d.a FROM (SELECT a, UPPER(s) AS s FROM t) AS d WHERE CAST(d.s AS CHAR(2)) = 'GA'",  # computed below
])
def test_other_casts_stay(sql):
    assert rewritten(sql) is None

def test_casts_stay_without_declared_types_or_in_an_unlisted_dialect():
    sql = "SELECT a FROM t WHERE CAST(s AS CHAR(2)) = 'GA'"
    assert rewritten(sql, types=None) is None
    assert rewritten(sql, types={}) is None
    assert rewritten(sql, dialect="oracle") is None
    assert rewritten(sql, dialect="bigquery") is None

CAST_TARGETS = [
    # Calcite's spelling of a CHAR column compared with literals
    ("SELECT a FROM t WHERE s IN ('GA', 'IL')", "SELECT a FROM t WHERE CAST(s AS CHAR(2)) IN ('GA', 'IL')"),
    ("SELECT a FROM t WHERE n = 'Wednesday'", "SELECT a FROM t WHERE CAST(n AS CHAR(9)) = 'Wednesday'"),
    # with the filter pushed into a derived table and the table's columns renamed by a column list
    (
        "SELECT t.a FROM t, u WHERE t.a = u.a AND u.s = 'GA' AND t.v <> 'x'",
        "SELECT t.a FROM (SELECT * FROM t WHERE CAST(v AS VARCHAR(20)) <> 'x') AS t INNER JOIN u AS u0 (a0, s0, v0) ON t.a = u0.a0 WHERE CAST(u0.s0 AS CHAR(2)) = 'GA'",
    ),
    ("SELECT s, COUNT(*) FROM t GROUP BY s", "SELECT CAST(s AS CHAR(2)), COUNT(*) FROM t GROUP BY CAST(s AS CHAR(2))"),
]

@pytest.mark.parametrize("left, right", CAST_TARGETS)
def test_identity_cast_pairs_prove(left, right):
    assert proved(left, right)

CAST_NEAR_MISSES = [
    # a number cast to a string compares as text
    ("SELECT a FROM t WHERE CAST(a AS VARCHAR(20)) < '5'", "SELECT a FROM t WHERE a < 5", {"t": [(10, "GA", "n", "v", "w")]}),
    # the cast of another column
    ("SELECT a FROM t WHERE CAST(v AS VARCHAR(20)) = 'GA'", "SELECT a FROM t WHERE s = 'GA'", {"t": [(1, "GA", "n", "v", "w")]}),
    # the same column of another table
    ("SELECT t.a FROM t, u WHERE CAST(u.s AS CHAR(2)) = 'GA'", "SELECT t.a FROM t, u WHERE t.s = 'GA'", {"t": [(1, "IL", "n", "v", "w")], "u": [(1, "GA", "v")]}),
]

def test_left_is_spelled_as_substring():
    assert rewritten("SELECT LEFT(v, 5) AS p FROM t") == "SELECT SUBSTRING(v FROM 1 FOR 5) AS p FROM t"
    assert rewritten("SELECT a FROM t WHERE LEFT(v, 0) = ''") == "SELECT a FROM t WHERE SUBSTRING(v FROM 1 FOR 0) = ''"
    assert rewritten("SELECT LEFT(v, 5) AS p FROM t", dialect="bigquery", types=None) == "SELECT SUBSTRING(v, 1, 5) AS p FROM t"
    assert rewritten("SELECT LEFT(CAST(s AS CHAR(2)), 1) AS p FROM t") is not None

@pytest.mark.parametrize("sql", [
    "SELECT LEFT(v, -2) AS p FROM t",  # all but the last two characters
    "SELECT LEFT(v, a) AS p FROM t",  # the length could be negative
    "SELECT LEFT(v, 1 + 1) AS p FROM t",
    "SELECT RIGHT(v, 5) AS p FROM t",
    "SELECT (SELECT LEFT(u.v, 2) FROM u LIMIT 1) AS p FROM t",  # belongs to the inner select
])
def test_other_prefix_calls_stay(sql):
    assert rewritten(sql) is None

LEFT_TARGETS = [
    ("SELECT t.a FROM t, u WHERE t.a = u.a AND SUBSTRING(t.v, 1, 5) <> SUBSTRING(u.v, 1, 5)", "SELECT t.a FROM t JOIN u ON t.a = u.a WHERE LEFT(t.v, 5) <> LEFT(u.v, 5)"),
    ("SELECT SUBSTR(v, 1, 2) AS p, COUNT(*) FROM t GROUP BY SUBSTR(v, 1, 2)", "SELECT LEFT(v, 2) AS p, COUNT(*) FROM t GROUP BY LEFT(v, 2)"),
    ("SELECT a FROM t WHERE SUBSTRING(v FROM 1 FOR 3) IN ('abc', 'abd')", "SELECT a FROM t WHERE LEFT(v, 3) IN ('abc', 'abd')"),
]

@pytest.mark.parametrize("left, right", LEFT_TARGETS)
def test_left_pairs_prove(left, right):
    assert proved(left, right)

STRINGS = {"t": [(1, "GA", "n", "hello", "w"), (2, "GA", "n", "hi", "w"), (3, "GA", "n", "", "w"), (4, "GA", "n", None, "w")]}

LEFT_NEAR_MISSES = [
    ("SELECT LEFT(v, 3) AS p FROM t", "SELECT SUBSTRING(v, 1, 2) AS p FROM t", STRINGS),  # another length
    ("SELECT LEFT(v, 3) AS p FROM t", "SELECT SUBSTRING(v, 2, 3) AS p FROM t", STRINGS),  # another start
    ("SELECT LEFT(v, 3) AS p FROM t", "SELECT RIGHT(v, 3) AS p FROM t", STRINGS),
    ("SELECT LEFT(v, -2) AS p FROM t", "SELECT SUBSTRING(v, 1, 2) AS p FROM t", STRINGS),  # all but the last two
    ("SELECT LEFT(v, 3) AS p FROM t", "SELECT v AS p FROM t", STRINGS),
]

REWRITTEN_QUERIES = [
    "SELECT a, LEFT(v, 0) AS p0, LEFT(v, 1) AS p1, LEFT(v, 3) AS p3, LEFT(v, 40) AS p40 FROM t",
    "SELECT LEFT(v, 2) AS p, COUNT(*) AS c FROM t GROUP BY LEFT(v, 2)",
    "SELECT t.a FROM t JOIN u ON LEFT(t.v, 1) = LEFT(u.v, 1) WHERE CAST(u.s AS CHAR(2)) <> 'GA' OR CAST(t.s AS CHAR(2)) IS NULL",
    "SELECT CAST(s AS CHAR(2)) AS s, CAST(v AS VARCHAR(20)) || '!' AS v, CAST(n AS CHAR(9)) = 'Sunday' AS m FROM t",
    "SELECT d.a FROM (SELECT k.a AS a, k.s AS s0 FROM t AS k) AS d WHERE CAST(d.s0 AS CHAR(2)) IN ('GA', 'IL') ORDER BY d.a",
]

def random_rows(rng):
    texts = [None, "", "a", "ab", "abc", "Sunday", "Sundays", "héllo wörld", "GA", "IL"]
    states = [None, "GA", "IL", "a", ""]
    t = [(rng.choice([None, 1, 2, 3]), rng.choice(states), rng.choice(texts), rng.choice(texts), rng.choice(texts)) for _ in range(rng.randint(0, 6))]
    u = [(rng.choice([None, 1, 2, 3]), rng.choice(states), rng.choice(texts)) for _ in range(rng.randint(0, 5))]
    return {"t": t + t[:2], "u": u + u[:1]}  # with duplicate rows

def test_normalize_applies_both_and_reaches_a_fixed_point():
    out = normalize("SELECT LEFT(CAST(s AS CHAR(2)), 1) AS p FROM t WHERE CAST(n AS CHAR(9)) = 'x'", schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "CAST" not in out and "LEFT" not in out and "SUBSTRING(" in out
