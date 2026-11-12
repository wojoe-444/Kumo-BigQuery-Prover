"""The exact rewrites of IN, BETWEEN and IS TRUE, the refined construct tags and the adjudication that uses them."""

import itertools
import json
from pathlib import Path

import pytest
import sqlglot

from kumo_bq_prover import trust
from kumo_bq_prover.constructs import constructs
from kumo_bq_prover.emulator import QueryError, running_emulator
from kumo_bq_prover.gap_rewrites import REWRITABLE, rewrite
from kumo_bq_prover.oracle import compare
from kumo_bq_prover.verdict import Verdict

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "gap_rewrites_bigquery.json").read_text())["rows"]

EXPRESSIONS = {
    "in_unnest": "x IN UNNEST(arr)",
    "notin_unnest": "x NOT IN UNNEST(arr)",
    "in_sub": "x IN (SELECT e FROM UNNEST(arr) AS e)",
    "notin_sub": "x NOT IN (SELECT e FROM UNNEST(arr) AS e)",
    "in_list": "x IN (a, b, c)",
    "notin_list": "x NOT IN (a, b)",
    "between": "x BETWEEN a AND b",
    "notbetween": "x NOT BETWEEN a AND b",
    "istrue": "(x > a) IS TRUE",
    "isnottrue": "(x > a) IS NOT TRUE",
    "isfalse": "(x > a) IS FALSE",
    "isnotfalse": "(x > a) IS NOT FALSE",
    "sub_in_where_corr": "(SELECT COUNT(*) FROM (SELECT 1 AS k) WHERE x IN UNNEST(arr)) = 1",
}


def _null(value):
    return "CAST(NULL AS INT64)" if value is None else str(value)


def _truth_table_query() -> str:
    """32 rows (x in NULL,1,2,3 against every subset of NULL,1,2) and each expression, rewritten, folded into one string."""
    rows = [(x, list(subset)) for x in (None, 1, 2, 3) for size in range(4) for subset in itertools.combinations((None, 1, 2), size)]
    selects = []
    for i, (x, arr) in enumerate(rows):
        a, b, c = (arr[k] if len(arr) > k else 9 for k in range(3))
        elements = ", ".join(_null(v) for v in arr)
        selects.append(f"SELECT {i} AS id, {_null(x)} AS x, ARRAY<INT64>[{elements}] AS arr, {_null(a)} AS a, {_null(b)} AS b, {_null(c)} AS c")
    parts = []
    for name, expression in EXPRESSIONS.items():
        rewritten = rewrite(f"SELECT {expression} AS r FROM t")
        assert rewritten, name
        item = sqlglot.parse_one(rewritten, read="bigquery").expressions[0]
        item = item.this if isinstance(item, sqlglot.exp.Alias) else item
        parts.append(f"(SELECT STRING_AGG(IFNULL(CAST(({item.sql(dialect='bigquery')}) AS STRING), 'N'), '' ORDER BY id) FROM t) AS r_{name}")
    return f"WITH t AS ({' UNION ALL '.join(selects)}) SELECT {', '.join(parts)}"


def test_the_rewrites_remove_every_gap_construct():
    sql = "SELECT a IN (1, 2), a NOT IN (SELECT x FROM u), a IN UNNEST(arr), a BETWEEN 1 AND 2, a IS TRUE, a IS NOT FALSE FROM t WHERE b IN (SELECT c FROM w WHERE c IN (1, NULL))"
    assert {"node:In", "node:Between", "is:boolean"} <= constructs(sql)
    assert not constructs(rewrite(sql)) & REWRITABLE


def test_a_cast_to_int64_is_written_with_round_and_a_rounded_cast_is_trusted():
    assert rewrite("SELECT CAST(a AS INT64), SAFE_CAST(b AS INT64) FROM t") == "SELECT CAST(ROUND(a) AS INT64), SAFE_CAST(ROUND(b) AS INT64) FROM t"
    assert rewrite("SELECT CAST(a AS STRING), CAST(3 AS INT64) FROM t") == "SELECT CAST(a AS STRING), CAST(3 AS INT64) FROM t"
    assert rewrite("SELECT CAST('2.5' AS INT64)") is None  # string parsing, not a float conversion
    assert trust.construct_hits("SELECT CAST(ROUND(a) AS INT64), CAST(ROUND(a, 0) AS INT64), CAST(7 AS INT64) FROM t") == []
    assert trust.construct_hits("SELECT CAST(ROUND(a, 1) AS INT64) FROM t") == ["cast:INT64"]  # still has a fraction to truncate
    assert trust.construct_hits("SELECT CAST(a AS INT64) FROM t") == ["cast:INT64"]


def test_the_rewrites_say_what_they_mean():
    assert rewrite("SELECT 1 FROM t WHERE a IN (1, 2)") == "SELECT 1 FROM t WHERE (a = 1 OR a = 2)"
    assert rewrite("SELECT 1 FROM t WHERE a BETWEEN 1 AND 2") == "SELECT 1 FROM t WHERE (a >= 1 AND a <= 2)"
    assert rewrite("SELECT a IS TRUE, a IS NOT TRUE FROM t") == "SELECT COALESCE(a, FALSE), NOT COALESCE(a, FALSE) FROM t"
    assert "EXISTS" in rewrite("SELECT 1 FROM t WHERE a IN (SELECT b FROM u)") and "ELSE FALSE" in rewrite("SELECT 1 FROM t WHERE a IN (SELECT b FROM u)")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT (a, b) IN ((1, 2)) FROM t",  # row-valued
        "SELECT a IN (SELECT 1 UNION ALL SELECT 2) FROM t",  # set operation
        "SELECT a IN (SELECT * FROM u) FROM t",  # SELECT * has no column to name
        "SELECT SUM(a) IN (SELECT b FROM u) FROM t",  # an aggregate would move into the subquery
        "SELECT RAND() IN (0.5, 0.25) FROM t",  # volatile
        "SELECT 1 FROM",  # does not parse
    ],
)
def test_what_cannot_be_rewritten_exactly_is_left_alone(sql):
    assert rewrite(sql) is None


def test_the_untouched_query_is_not_rewritten_into_something_else():
    assert not constructs("SELECT a FROM t WHERE a > 1") & REWRITABLE
    assert rewrite("SELECT a FROM t WHERE a > 1") == "SELECT a FROM t WHERE a > 1"


def test_extract_week_is_untrusted_and_the_other_parts_are_not():
    assert "extract:WEEK" in trust.construct_hits("SELECT EXTRACT(WEEK FROM d) FROM t")
    assert "extract:WEEK" in trust.construct_hits("SELECT EXTRACT(WEEK(MONDAY) FROM d) FROM t")
    for part in ("YEAR", "QUARTER", "MONTH", "DAY", "DAYOFWEEK", "DAYOFYEAR", "ISOYEAR", "ISOWEEK", "HOUR", "MINUTE", "SECOND"):
        assert trust.construct_hits(f"SELECT EXTRACT({part} FROM d) FROM t") == [], part


def test_like_is_trusted_for_a_literal_pattern_of_ordinary_characters():
    assert trust.construct_hits("SELECT 1 FROM t WHERE s LIKE '%abc%'", facts=set()) == []
    assert trust.construct_hits("SELECT 1 FROM t WHERE s LIKE 'a_c'") == ["like:wild"]
    assert trust.construct_hits("SELECT 1 FROM t WHERE s LIKE p") == ["like:wild"]
    assert "like:wild" in trust.construct_hits(r"SELECT 1 FROM t WHERE s LIKE r'100\%'")
    assert "like:wild" in trust.construct_hits("SELECT 1 FROM t WHERE s LIKE ANY ('a%', 'b%')")


def test_like_on_a_newline_and_substr_on_non_ascii_text_are_untrusted_only_with_such_text():
    like = "SELECT 1 FROM t WHERE s LIKE '%abc%'"
    assert trust.untrusted_reasons(like, like, setup=["INSERT INTO t VALUES ('a b')"]) == []
    assert trust.untrusted_reasons(like, like, setup=["INSERT INTO t VALUES ('a\nb')"]) == ["like:plain"]
    substr = "SELECT SUBSTR(s, 2) FROM t"
    assert trust.untrusted_reasons(substr, substr, setup=["INSERT INTO t VALUES ('hello')"]) == []
    assert trust.untrusted_reasons(substr, substr, setup=["INSERT INTO t VALUES ('héllo')"]) == ["fn:SUBSTRING"]
    assert trust.untrusted_reasons("SELECT STRPOS(s, 'l') FROM t", "SELECT 1 FROM t") == []
    assert trust.untrusted_reasons("SELECT STRPOS(s, 'l') FROM t", "SELECT 1 FROM t", left_rows=[("日本",)]) == ["fn:STR_POSITION"]
    assert "fn:SUBSTRING" in trust.construct_hits(substr)  # no witness: the cautious reading


def test_format_directives_are_trusted_only_when_checked_on_bigquery():
    assert trust.construct_hits("SELECT FORMAT_DATE('%Y-%m-%d', d) FROM t") == []
    assert trust.construct_hits("SELECT FORMAT_TIMESTAMP('%H:%M:%E3S', t) FROM t") == []
    for bad in ("%j", "%e", "%U", "%W", "%w", "%z", "%g", "%E*S", "%c", "%-d", "%V", "%O", "%Ey"):
        assert trust.construct_hits(f"SELECT FORMAT_DATE('{bad}', d) FROM t") == ["format:unverified"], bad
    from kumo_bq_prover.constructs import VERIFIED_FORMAT_DIRECTIVES

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":  # nothing outside the checked set is trusted, whatever sqlglot rewrites it to
        tags = constructs(f"SELECT FORMAT_DATE('%{letter}', d) FROM t")
        assert ("format:verified" in tags) == (letter in VERIFIED_FORMAT_DIRECTIVES), letter
    assert trust.construct_hits("SELECT FORMAT_DATE(f, d) FROM t") == ["format:unverified"]


def test_date_diff_in_iso_weeks_is_untrusted():
    assert trust.construct_hits("SELECT DATE_DIFF(a, b, ISOWEEK) FROM t") == ["date_diff:ISOWEEK"]
    assert trust.construct_hits("SELECT DATE_DIFF(a, b, DAY) FROM t") == []
    assert trust.construct_hits("SELECT DATE_DIFF(a, b, WEEK) FROM t") == []


def test_volatile_and_zone_constructs_are_untrusted():
    for sql in ("SELECT CURRENT_DATE()", "SELECT CURRENT_TIMESTAMP()", "SELECT GENERATE_UUID()", "SELECT EXTRACT(DATE FROM t AT TIME ZONE 'UTC') FROM t"):
        assert trust.construct_hits(sql) != [], sql


class Runner:
    """Answers the rewritten queries with canned rows (``rewritten`` maps a substring of the SQL to its rows)."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.seen = rows, fail, []

    def __call__(self, sql):
        self.seen.append(sql)
        if self.fail:
            raise QueryError("no")
        return self.rows[len(self.seen) - 1]


def test_a_difference_without_gap_constructs_is_trusted_as_it_is():
    verdict = trust.adjudicate(Runner([]), "SELECT a FROM t", "SELECT b FROM t", [], [(1,)], [(2,)])
    assert verdict.trusted and not verdict.rewritten and verdict.left_rows == [(1,)]


def test_an_in_difference_that_survives_the_rewrite_is_trusted_with_the_rewritten_rows():
    runner = Runner([[(10,)], [(20,)]])
    verdict = trust.adjudicate(runner, "SELECT a FROM t WHERE a IN (1, 2)", "SELECT a FROM t WHERE a IN (1, 3)", [], [(1,)], [(2,)])
    assert verdict.trusted and verdict.rewritten and verdict.left_rows == [(10,)] and verdict.right_rows == [(20,)]
    assert all(" IN " not in sql for sql in runner.seen)


def test_an_in_difference_that_disappears_in_the_rewrite_is_the_emulators_gap():
    verdict = trust.adjudicate(Runner([[(1,)], [(1,)]]), "SELECT 3 IN (1, 2, NULL) AS x", "SELECT CAST(NULL AS BOOL) AS x", [], [(False,)], [(None,)])
    assert not verdict.trusted and "node:In" in verdict.reasons[0] and "disappears" in verdict.reasons[0]


def test_a_gap_construct_next_to_an_untrusted_one_stays_untrusted_without_a_rerun():
    runner = Runner([])
    verdict = trust.adjudicate(runner, "SELECT CAST(f AS NUMERIC) FROM t WHERE a IN (1)", "SELECT 2 FROM t", [], [(1,)], [(2,)])
    assert not verdict.trusted and "cast:NUMERIC" in verdict.reasons and runner.seen == []


def test_a_cast_difference_is_rechecked_with_round_and_refused_for_integers_from_two_to_the_53():
    runner = Runner([[(3,)], [(4,)]])
    verdict = trust.adjudicate(runner, "SELECT CAST(f AS INT64) FROM t", "SELECT 4 FROM t", ["INSERT INTO t VALUES (2.5)"], [(2,)], [(4,)])
    assert verdict.trusted and verdict.rewritten and verdict.left_rows == [(3,)]
    assert "ROUND" in runner.seen[0]
    runner = Runner([])
    verdict = trust.adjudicate(runner, "SELECT CAST(f AS INT64) FROM t", "SELECT 4 FROM t", ["INSERT INTO t VALUES (9007199254740993)"], [(9007199254740993,)], [(4,)])
    assert not verdict.trusted and "value:int-above-2^53" in verdict.reasons and runner.seen == []


def test_a_rewrite_the_emulator_rejects_leaves_the_difference_withheld():
    verdict = trust.adjudicate(Runner([], fail=True), "SELECT a FROM t WHERE a IN (1)", "SELECT a FROM t", [], [(1,)], [(2,)])
    assert not verdict.trusted and verdict.reasons == ["node:In"]


def test_a_query_that_cannot_be_rewritten_stays_withheld():
    verdict = trust.adjudicate(Runner([]), "SELECT (a, b) IN ((1, 2)) FROM t", "SELECT 1 FROM t", [], [(True,)], [(1,)])
    assert not verdict.trusted and verdict.reasons == ["node:In"]


@pytest.mark.emulator
def test_the_rewritten_forms_give_the_answers_bigquery_gave():
    """GOLDEN holds BigQuery's rows for the original IN, BETWEEN and IS TRUE expressions (the rewritten forms matched them there too)."""
    with running_emulator() as client:
        result = client.query(_truth_table_query())
    names = [field["name"] for field in result.schema["fields"]]
    got = dict(zip(names, result.rows[0]))
    for name, expected in GOLDEN.items():
        assert got[f"r_{name}"] == expected, name


@pytest.mark.emulator
def test_a_refuted_pair_that_differs_through_in_is_recovered_on_the_emulator():
    setup = [
        "CREATE TABLE t (a INT64)",
        "INSERT INTO t VALUES (1), (2), (3)",
        "CREATE TABLE u (b INT64)",
        "INSERT INTO u VALUES (2), (NULL)",
    ]
    left = "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)"
    right = "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)"
    with running_emulator() as client:
        result = compare(client, left, right, setup=setup)
    assert result.verdict is Verdict.REFUTED and result.left_rows == [] and sorted(result.right_rows) == [(1,), (3,)]


@pytest.mark.emulator
def test_a_between_pair_with_a_null_bound_is_not_a_false_refutation():
    left, right = "SELECT 10 BETWEEN NULL AND 5 AS x", "SELECT FALSE AS x"
    with running_emulator() as client:
        assert client.query(left).rows != client.query(right).rows  # the emulator's gap
        result = compare(client, left, right)
    assert result.verdict is Verdict.UNKNOWN and "node:Between" in result.untrusted[0]
