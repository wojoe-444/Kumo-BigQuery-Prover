import contextlib

from kumo_bq_prover import trust
from kumo_bq_prover.constructs import constructs
from kumo_bq_prover.emulator import QueryResult
from kumo_bq_prover.oracle import compare
from kumo_bq_prover.verdict import Verdict


def test_constructs_tags_functions_casts_joins_and_booleans():
    tags = constructs("SELECT CAST(f AS INT64), SAFE_DIVIDE(a, b), x IS TRUE FROM t LEFT JOIN UNNEST(arr) AS e")
    assert {"cast:INT64", "fn:SAFE_DIVIDE", "is:boolean", "join:LEFT:UNNEST"} <= tags


def test_unparsable_sql_is_never_trusted():
    assert constructs("SELEC FROM WHERE (") == {"unparsed"}
    assert trust.construct_hits("SELEC FROM WHERE (") == ["unparsed"]


def test_known_divergences_are_untrusted():
    assert "cast:INT64" in trust.construct_hits("SELECT CAST(f AS INT64) FROM t")
    assert "join:LEFT:UNNEST" in trust.construct_hits("SELECT 1 FROM t LEFT JOIN UNNEST(arr) AS e")
    assert "node:In" in trust.construct_hits("SELECT x IN (1, NULL) FROM t")


def test_plain_queries_are_trusted():
    assert trust.construct_hits("SELECT a, SUM(b) FROM t WHERE a > 1 GROUP BY a") == []


def test_value_hits_find_special_and_extreme_values():
    assert "value:large-number" in trust.value_hits(["INSERT INTO t VALUES (1e19)"])
    assert "value:nan-or-infinity" in trust.value_hits(queries=["SELECT IEEE_DIVIDE(1, 0)"])
    assert "value:negative-zero" in trust.value_hits(["INSERT INTO t VALUES (-0.0)"])
    assert "value:wide-decimal" in trust.value_hits(["INSERT INTO t VALUES (NUMERIC '0.0000000001')"])
    assert "value:nan-or-infinity" in trust.value_hits(rows=[[(float("inf"),)]])
    assert "value:wide-decimal" not in trust.value_hits(["INSERT INTO t VALUES (NUMERIC '0.000000001')"])


def test_float_values_around_two_to_the_53_are_trusted():
    assert trust.value_hits(["INSERT INTO t VALUES (9007199254740993, 9.007199254740992e15)"], rows=[[(9007199254740992.0,)]]) == []


def test_ordinary_values_and_timestamps_are_trusted():
    assert trust.value_hits(["INSERT INTO t VALUES (1, 2.5, 'x')"], ["SELECT a FROM t"], [[(1, 1771000000000000)]]) == []


class FakeClient:
    """Answers the setup with nothing and each query with the rows it was given."""

    def __init__(self, left_rows, right_rows):
        self.answers = [left_rows, right_rows]

    @contextlib.contextmanager
    def scratch_dataset(self):
        yield "d"

    def query(self, sql, dataset=None, params=None, decode=True):
        if sql.startswith("SETUP"):
            return QueryResult(schema={}, rows=[])
        return QueryResult(schema={}, rows=self.answers.pop(0))


def test_a_difference_on_trusted_ground_is_refuted():
    result = compare(FakeClient([(1,)], [(2,)]), "SELECT a FROM t", "SELECT b FROM t", setup=["SETUP"])
    assert result.verdict is Verdict.REFUTED and result.untrusted == []


def test_a_difference_on_a_known_divergence_is_unknown_with_the_reason():
    result = compare(FakeClient([(2,)], [(3,)]), "SELECT CAST(f AS INT64) FROM t", "SELECT 3 FROM t", setup=["SETUP"])
    assert result.verdict is Verdict.UNKNOWN
    assert "cast:INT64" in result.untrusted and "not trusted" in result.reason
    assert result.left_rows == [(2,)]  # the emulator's rows stay available for diagnosis


def test_a_difference_built_from_extreme_values_is_unknown():
    result = compare(FakeClient([(1,)], [(2,)]), "SELECT a FROM t", "SELECT b FROM t", setup=["SETUP INSERT 1e300"])
    assert result.verdict is Verdict.UNKNOWN and "value:large-number" in result.untrusted


def test_the_guard_can_be_switched_off_to_measure_it():
    result = compare(FakeClient([(2,)], [(3,)]), "SELECT CAST(f AS INT64) FROM t", "SELECT 3 FROM t", setup=["SETUP"], trust=False)
    assert result.verdict is Verdict.REFUTED
