import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from audit_proofs import declared_schema, emulator_schema  # noqa: E402

from kumo_bq_prover.infer import infer_schema  # noqa: E402


def test_infer_reads_tables_columns_and_literal_types():
    schema = infer_schema(["SELECT a.x, b.name FROM t AS a JOIN u AS b ON a.id = b.id WHERE b.name = 'z' AND a.v > 1.5"])
    assert schema == {"t": {"x": "INT64", "id": "INT64", "v": "FLOAT64"}, "u": {"name": "STRING", "id": "INT64"}}


def test_infer_skips_ctes_aliases_and_unnest_names():
    schema = infer_schema(["WITH c AS (SELECT x FROM t) SELECT c.x, e FROM c CROSS JOIN UNNEST([1, 2]) AS e WHERE e IS NOT NULL"])
    assert schema == {"t": {"x": "INT64"}}


def test_infer_returns_nothing_for_text_that_does_not_parse():
    assert infer_schema(["SELECT FROM WHERE"]) == {}


def test_a_declared_table_keeps_exactly_its_declared_columns_in_order():
    """A column the declaration lacks would change how a bare name resolves, and the order decides SELECT *."""

    pair = {"left": "SELECT * FROM t WHERE x > 1", "right": "SELECT id, x FROM t WHERE 1 < x", "schema": {"t": ["id", "x"]}}
    assert list(emulator_schema(pair, infer_schema)["t"]) == ["id", "x"]
    correlated = {"left": "SELECT a.x FROM t AS a WHERE EXISTS (SELECT 1 FROM u WHERE x = 2)", "right": "SELECT 1", "schema": {"t": ["x"], "u": ["k"]}}
    assert emulator_schema(correlated, infer_schema)["u"] == {"k": "INT64"}


def test_declared_types_win_over_guesses():
    pair = {"left": "SELECT x FROM t WHERE x > 1", "right": "SELECT x FROM t", "types": {"t": {"x": "FLOAT64"}}}
    assert emulator_schema(pair, infer_schema) == {"t": {"x": "FLOAT64"}}
    assert declared_schema(pair) == {"t": {"x": "FLOAT64"}}
