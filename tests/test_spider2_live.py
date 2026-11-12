"""Live check of the Spider 2.0 pair pipeline on a synthetic query. Run with: pytest -m emulator"""

import pytest

from kumo_bq_prover import spider2
from kumo_bq_prover.emulator import running_emulator

pytestmark = pytest.mark.emulator

QUERY = """
SELECT ep.value.string_value AS page, COUNT(*) AS n
FROM `some-project.some_dataset.events_*` AS e, UNNEST(e.event_params) AS ep
WHERE ep.key = 'page_title'
  AND _TABLE_SUFFIX >= '20210101' AND _TABLE_SUFFIX <= '20210107'
  AND e.event_name = 'page_view'
GROUP BY page
ORDER BY n DESC
LIMIT 5
"""


@pytest.fixture(scope="module")
def run():
    with running_emulator() as client:
        yield spider2.run_query(client, "synthetic", sql=QUERY, databases=12)


def test_a_ga4_shaped_query_runs_and_yields_pairs(run):
    assert run.status == "ok", run.reason
    assert any(p["kind"] == "rewrite" for p in run.pairs) and any(p["kind"] == "mutation" for p in run.pairs)


def test_the_generated_data_reaches_the_query(run):
    assert run.databases_nonempty > 0


def test_no_rewrite_is_refuted_and_refuted_mutations_carry_a_witness(run):
    assert not [p for p in run.pairs if p["oracle"] == "WRONG"]
    refuted = [p for p in run.pairs if p["label"] == "not_equivalent"]
    assert refuted
    for pair in refuted:
        witness = pair["witness"]
        assert witness["setup"]
        rows = lambda key: sorted(str(r) for r in witness[key])  # noqa: E731
        assert rows("left_rows") != rows("right_rows") or witness["left_row_count"] != witness["right_row_count"]
