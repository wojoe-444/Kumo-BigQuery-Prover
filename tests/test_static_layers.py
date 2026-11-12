"""The static proof layers as the public entry point uses them: offline, no emulator."""

import pytest

from kumo_bq_prover.prove import prove_equivalent
from kumo_bq_prover.static import LAYERS, cannot_fail, float_aggregate_exposure, prove_static_detailed
from kumo_bq_prover.verdict import Verdict

SCHEMA = {"t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "s": "STRING"}, "events": {"user_id": "INT64", "ts": "INT64", "value": "INT64"}}


def detail(left, right, **options):
    return prove_static_detailed(left, right, SCHEMA, **options)


def test_identical_queries_are_proved_by_the_first_layer():
    result = detail("SELECT x FROM t", "SELECT x FROM t")
    assert (result.verdict, result.layer) == (Verdict.PROVEN, "identical")


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT x FROM t WHERE x > 1 AND y < 2", "SELECT x FROM t WHERE y < 2 AND x > 1"),
        ("SELECT CASE WHEN x IS NULL THEN 0 ELSE x END AS v FROM t", "SELECT COALESCE(x, 0) AS v FROM t"),
        ("SELECT x FROM t WHERE x BETWEEN 1 AND 3", "SELECT x FROM t WHERE x >= 1 AND x <= 3"),
        ("SELECT x FROM t UNION DISTINCT SELECT y FROM t", "SELECT DISTINCT x FROM (SELECT x FROM t UNION ALL SELECT y FROM t)"),
    ],
)
def test_sound_rewrites_are_proved(left, right):
    assert detail(left, right).verdict is Verdict.PROVEN


@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT x FROM t WHERE x > 1", "SELECT x FROM t WHERE x >= 1"),
        ("SELECT x FROM t", "SELECT DISTINCT x FROM t"),
        ("SELECT x FROM t WHERE x IN (1, 2)", "SELECT x FROM t WHERE x IN (1, 3)"),
        ("SELECT COUNT(*) AS n FROM t", "SELECT COUNT(x) AS n FROM t"),
    ],
)
def test_different_queries_are_not_proved(left, right):
    assert detail(left, right).verdict is Verdict.UNKNOWN


def test_a_global_aggregate_inside_a_window_is_not_pruned_away():
    """Found by the pair corpora: Kumo_SQL_Prover proved this pair. The left returns one row, the right one per event."""

    left = "SELECT x FROM (SELECT 1 AS x, SUM(COUNT(*)) OVER () AS w FROM events) AS d"
    right = "SELECT 1 AS x FROM events"
    assert detail(left, right).verdict is Verdict.UNKNOWN


def test_float_sums_regrouped_into_partial_sums_are_not_proved():
    """1e16 + 1 + 1 + 1 is not (1e16 + 1) + (1 + 1) in FLOAT64; the algebraic layer reads sums as real numbers."""

    left = "SELECT SUM(f) AS v FROM t"
    right = "SELECT SUM(s) AS v FROM (SELECT SUM(f) AS s FROM t GROUP BY x)"
    result = detail(left, right)
    assert result.verdict is Verdict.UNKNOWN
    assert "FLOAT64" in result.reason


def test_integer_sums_regroup_when_the_types_are_known():
    left = "SELECT SUM(x) AS v FROM t"
    right = "SELECT SUM(s) AS v FROM (SELECT SUM(x) AS s FROM t GROUP BY y)"
    assert float_aggregate_exposure_of(left, right) is None


def float_aggregate_exposure_of(left, right):
    import sqlglot

    from kumo_bq_prover.static import solver_inputs

    trees = [sqlglot.parse_one(sql, read="bigquery") for sql in (left, right)]
    return float_aggregate_exposure(trees, solver_inputs(SCHEMA)["types"])


@pytest.mark.parametrize(
    "sql",
    ["SELECT SUM(f) FROM t", "SELECT AVG(x) FROM t", "SELECT SUM(x / y) FROM t", "SELECT SUM(x * 1.5) FROM t", "SELECT SUM(unknown_column) FROM t"],
)
def test_possible_float_aggregate_inputs_are_flagged(sql):
    assert float_aggregate_exposure_of(sql, sql) is not None


def test_ordered_comparison_is_only_proved_when_identical():
    left, right = "SELECT x FROM t WHERE x > 1 AND y < 2", "SELECT x FROM t WHERE y < 2 AND x > 1"
    assert detail(left, right, ordered=True).verdict is Verdict.UNKNOWN
    assert detail(left, left, ordered=True).verdict is Verdict.PROVEN


def test_a_rewrite_that_can_raise_where_the_original_does_not_is_withheld():
    left = "SELECT x / y AS q FROM t WHERE y <> 0"
    right = "SELECT x / y AS q FROM t"
    result = detail(left, right)
    assert result.verdict is Verdict.UNKNOWN


def test_cannot_fail_syntax_is_narrow():
    import sqlglot

    assert cannot_fail(sqlglot.parse_one("SELECT a FROM t WHERE b = 1 AND c IS NOT NULL", read="bigquery"))
    for sql in ("SELECT a + 1 FROM t", "SELECT CAST(a AS INT64) FROM t", "SELECT (SELECT MAX(b) FROM u) FROM t", "SELECT SUM(a) FROM t"):
        assert not cannot_fail(sqlglot.parse_one(sql, read="bigquery")), sql


def test_a_query_that_does_not_parse_is_unknown():
    assert detail("SELECT FROM WHERE", "SELECT 1").verdict is Verdict.UNKNOWN


def test_layers_can_be_chosen():
    left, right = "SELECT x FROM t WHERE x > 1 AND y < 2", "SELECT x FROM t WHERE y < 2 AND x > 1"
    assert detail(left, right, layers=("identical",)).verdict is Verdict.UNKNOWN
    assert detail(left, right, layers=LAYERS).verdict is Verdict.PROVEN


def test_public_entry_point_returns_the_layer_and_assumptions_without_an_emulator():
    result = prove_equivalent("SELECT x FROM t WHERE x > 1 AND y < 2", "SELECT x FROM t WHERE y < 2 AND x > 1", schema=SCHEMA)
    assert result.verdict is Verdict.PROVEN
    assert result.reason.split(":")[0] in LAYERS
    assert isinstance(result.assumptions, tuple)
    unknown = prove_equivalent("SELECT x FROM t", "SELECT y FROM t", schema=SCHEMA)
    assert unknown.verdict is Verdict.UNKNOWN


def test_identical_text_is_not_proved_when_the_query_can_answer_differently_on_two_runs():
    """Found by the proof audit: two runs of an approximate aggregate with tied counts return different values."""

    approximate = "SELECT APPROX_TOP_COUNT(id, 2) AS x FROM t"
    assert detail(approximate, approximate).verdict is Verdict.UNKNOWN
    assert detail("SELECT x FROM t", "SELECT x FROM t").verdict is Verdict.PROVEN
