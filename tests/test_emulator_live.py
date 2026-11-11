"""Live checks against the real emulator binary. Run with: pytest -m emulator

These fail (never skip) when the binary is missing: a check that cannot run is not a pass.
"""

import pytest

from kumo_bq_prover.emulator import running_emulator
from kumo_bq_prover.oracle import compare
from kumo_bq_prover.verdict import Verdict

pytestmark = pytest.mark.emulator


@pytest.fixture(scope="module")
def client():
    with running_emulator() as c:
        yield c


def test_select_one(client):
    assert client.query("SELECT 1 AS x").rows == [(1,)]


def test_struct_and_array_round_trip(client):
    result = client.query("SELECT STRUCT(1 AS a, 'x' AS b) AS s, [1, 2, 3] AS arr")
    assert result.rows == [(("STRUCT", (("a", 1), ("b", "x"))), ("ARRAY", (1, 2, 3)))]


def test_oracle_refutes_a_real_difference(client):
    result = compare(
        client,
        "SELECT x FROM UNNEST([1, 2, 3]) AS x",
        "SELECT x FROM UNNEST([1, 2]) AS x",
    )
    assert result.verdict is Verdict.REFUTED
