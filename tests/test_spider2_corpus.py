import hashlib
import json
import pathlib

import pytest
import sqlglot

from kumo_bq_prover import spider2

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPIDER = ROOT / "benchmarks" / "spider2"
PAIRS = ROOT / "benchmarks" / "pairs"
LABELS = {"equivalent", "not_equivalent", "unlabelled"}


def test_gold_queries_are_the_vendored_upstream_files_with_their_licence():
    cases = spider2.manifest()
    assert len(cases) == 142
    for case in cases:
        data = (SPIDER / "gold" / f"{case['id']}.sql").read_bytes()
        assert hashlib.sha256(data).hexdigest() == case["sha256"], case["id"]
    licence = (SPIDER / "LICENSE-Spider2").read_text()
    assert licence.startswith("MIT License") and "bird_sql" in licence


def test_split_is_every_fifth_task_by_hash_and_recorded_in_the_manifest():
    cases = spider2.manifest()
    held_out = [c["id"] for c in cases if c["split"] == "held-out"]
    assert len(held_out) == 30 and len(cases) - len(held_out) == 112
    assert all(spider2.split_of(c["id"]) == c["split"] for c in cases)


def pair_files():
    return sorted(PAIRS.glob("spider2-bigquery-*.jsonl"))


@pytest.mark.parametrize("path", pair_files() or [None], ids=lambda p: p.name if p else "none")
def test_generated_pairs_are_well_formed(path):
    if path is None:
        pytest.skip("no generated pair file yet")
    split = path.stem.removeprefix("spider2-bigquery-")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert rows and len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        assert spider2.split_of(row["query"]) == split, "a pair sits in the wrong split file"
        assert row["label"] in LABELS and row["kind"] in ("rewrite", "mutation")
        assert row["oracle"] != "WRONG", f"{row['id']}: a rewrite was refuted; look at it by hand"
        if row["kind"] == "rewrite":
            assert row["label"] == "equivalent"
        if row["label"] == "not_equivalent":
            witness = row["witness"]
            if max(witness["left_row_count"], witness["right_row_count"]) <= spider2.WITNESS_ROWS:
                as_multiset = lambda rows: sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)  # noqa: E731
                assert as_multiset(witness["left_rows"]) != as_multiset(witness["right_rows"]), row["id"]
        sqlglot.parse_one(row["left"], read="bigquery")
        sqlglot.parse_one(row["right"], read="bigquery")
        assert "bigquery-public-data" not in row["left"], "tables must be renamed to t_N"
