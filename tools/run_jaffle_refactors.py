#!/usr/bin/env python3
"""Score the emulator oracle on the Jaffle Shop refactors (benchmarks/pairs/jaffle-shop-refactors.jsonl).

Each case is a refactor of a five-model dbt project: the models before and after, rendered
to BigQuery SQL in dependency order. For every database (the upstream seeds, an authored
edge database and N seeded random databases over the seed tables) both pipelines are built
on the emulator as ``CREATE TABLE name AS <model>``, and every model that exists in both
is compared as a multiset (FLOAT64 columns rounded to 6 digits, because a FLOAT64 sum has no
fixed order). A model that differs on any database refutes the refactor.

  equivalent label      ok | WRONG (a model differs: an emulator bug or a bad label) | emulator_error
  not_equivalent label  caught | missed | emulator_error

The "before" pipeline is built once per database and reused for every case. Exits 1 on WRONG.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover.datagen import insert_statement  # noqa: E402
from kumo_bq_prover.emulator import QueryError, running_emulator  # noqa: E402
from kumo_bq_prover.values import multiset  # noqa: E402

PAIRS = ROOT / "benchmarks/pairs"
STATUSES = ["placed", "shipped", "completed", "return_pending", "returned", None]
METHODS = ["credit_card", "coupon", "bank_transfer", "gift_card", "cash", None]
DATES = ["2018-01-01", "2018-01-02", "2018-02-01", None]
FLOAT_DIGITS = 6


def random_rows(rng: random.Random) -> dict[str, list[list]]:
    """A small database over the seed tables: NULLs, duplicate and dangling keys, empty tables."""

    def ident(top: int):
        return None if rng.random() < 0.12 else rng.randint(1, top)

    def count():
        return rng.choice([0, 1, 2, 3, 4, 5, 6, 8])

    data = {
        "raw_customers": [[ident(4), rng.choice(["Ann", "Bo", None]), rng.choice(["P.", None])] for _ in range(count())],
        "raw_orders": [[ident(5), ident(4), rng.choice(DATES), rng.choice(STATUSES)] for _ in range(count())],
        "raw_payments": [
            [ident(6), ident(5), rng.choice(METHODS), rng.choice([None, 0, 100, 250, 1000, -50])] for _ in range(count())
        ],
    }
    for rows in data.values():
        if rows and rng.random() < 0.3:
            rows.append(rng.choice(rows))
    return data


def databases(seeds: dict, trials: int) -> list[tuple[str, dict]]:
    rng = random.Random(11)
    return (
        [("seeds", seeds["seeds"]), ("edge", seeds["edge"])]
        + [(f"random-{i}", random_rows(rng)) for i in range(trials)]
    )


def build(client, dataset: str, schema: dict, rows: dict, models: list[list[str]]) -> dict[str, collections.Counter]:
    for table, columns in schema.items():
        client.query(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in columns.items())})", dataset)
        if rows.get(table):
            client.query(insert_statement(table, columns, rows[table]), dataset)
    out = {}
    for name, sql in models:
        client.query(f"CREATE TABLE {name} AS {sql}", dataset)
    for name, _ in models:
        result = client.query(f"SELECT * FROM {name}", dataset)
        out[name] = multiset([tuple(round(c, FLOAT_DIGITS) if isinstance(c, float) else c for c in row) for row in result.rows])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--databases", type=int, default=12, help="random databases (the seeds and the edge database always run)")
    parser.add_argument("--only", help="run only case ids containing this text")
    parser.add_argument("--json", help="write per-case outcomes to this file")
    args = parser.parse_args()
    cases = [json.loads(line) for line in (PAIRS / "jaffle-shop-refactors.jsonl").read_text().splitlines() if line]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    seeds = json.loads((PAIRS / "jaffle-shop/seeds.json").read_text())
    dbs = databases(seeds, args.databases)
    state = {c["id"]: {"refuted": None, "errors": collections.Counter(), "agreed": 0} for c in cases}
    base = cases[0]["before"] if cases else []
    for db_name, rows in dbs:
        # a fresh emulator per database: it slows down as datasets accumulate
        with running_emulator() as client:
            try:
                with client.scratch_dataset() as dataset:
                    before = build(client, dataset, seeds["schema"], rows, base)
            except QueryError as error:
                for s in state.values():
                    s["errors"][str(error)[:120]] += 1
                continue
            for case in cases:
                s = state[case["id"]]
                if s["refuted"]:
                    continue
                try:
                    with client.scratch_dataset() as dataset:
                        after = build(client, dataset, seeds["schema"], rows, case["after"])
                except QueryError as error:
                    s["errors"][str(error)[:120]] += 1
                    continue
                differing = [n for n in before if n in after and before[n] != after[n]]
                if differing:
                    s["refuted"] = {"database": db_name, "models": differing}
                else:
                    s["agreed"] += 1
            print(f"database {db_name} done", flush=True)
    outcomes = []
    for case in cases:
        s = state[case["id"]]
        if case["label"] == "equivalent":
            outcome = "WRONG" if s["refuted"] else ("emulator_error" if not s["agreed"] else "ok")
        else:
            outcome = "caught" if s["refuted"] else ("emulator_error" if not s["agreed"] else "missed")
        outcomes.append(
            {"id": case["id"], "label": case["label"], "outcome": outcome, "databases_agreed": s["agreed"],
             "refuted_on": s["refuted"], "errors": dict(s["errors"])}
        )
        print(f"{outcome:15} {case['label']:15} {case['id']:36} {s['refuted'] or next(iter(s['errors']), '')}")
    counts = collections.Counter((o["label"], o["outcome"]) for o in outcomes)
    print("\n" + ", ".join(f"{label}/{outcome}: {n}" for (label, outcome), n in sorted(counts.items())))
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps({"counts": {f"{a}/{b}": n for (a, b), n in counts.items()}, "cases": outcomes}, indent=1, default=str)
        )
    return 1 if any(o["outcome"] == "WRONG" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
