#!/usr/bin/env python3
"""Score the emulator oracle on a labelled pair corpus in benchmarks/pairs/.

For every case the two queries run on N seeded random databases (plus the
case's own witness rows when it has them). Outcomes:

  equivalent label      ok | WRONG (refuted: an emulator bug or a bad label) | emulator_error
  not_equivalent label  caught (refuted) | missed | incomplete (not refuted, but the emulator failed
                        on some databases) | emulator_error
  unresolved label      refuted | not_refuted | emulator_error (the label is decided by this run)
  any other label       unscored

A case with a "database" field (the sample-database corpus) runs on databases generated under
the declared keys of benchmarks/pairs/schemas/<database>.sql, minus the case's "drop" list, plus
its own witness rows completed with legal values (see constrained.py).

The script exits 1 when any case is WRONG. Emulator rejections are grouped by
message at the end: that list is the emulator's gap list.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover import constrained  # noqa: E402
from kumo_bq_prover.datagen import fixed_database, random_database  # noqa: E402
from kumo_bq_prover.emulator import running_emulator  # noqa: E402
from kumo_bq_prover.harvest import harvest  # noqa: E402
from kumo_bq_prover.oracle import compare  # noqa: E402
from kumo_bq_prover.static import prove_static  # noqa: E402
from kumo_bq_prover.verdict import Verdict  # noqa: E402

RECYCLE = 6

NUMERIC_TRAPS_SCHEMA = {
    "t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "g": "FLOAT64", "n": "NUMERIC", "m": "NUMERIC", "s": "STRING", "c": "BOOL"}
}


def schema_of(case: dict) -> dict:
    return case.get("schema") or NUMERIC_TRAPS_SCHEMA


def classify_error(reason: str) -> str:
    text = re.sub(r"\s+", " ", reason)
    match = re.search(r"no such function: (\w+)", text)
    if match:
        return f"no such function: {match.group(1)}"
    message = re.search(r'"message":"([^"]{0,160})', text)
    return (message.group(1) if message else text[:160]).replace("\\", "")


UNSAFE_SCHEMA = {"t": {c: "INT64" for c in "abc"}, "u": {c: "INT64" for c in "abc"}}
_DDL: dict[str, dict] = {}


def tables_of(database: str) -> dict:
    if database not in _DDL:
        _DDL[database] = constrained.parse_ddl((ROOT / "benchmarks/pairs/schemas" / f"{database}.sql").read_text())
    return _DDL[database]


def constrained_setups(case: dict, databases: int, use_harvest: bool = True) -> list[list[str]]:
    tables = tables_of(case["database"])
    names = constrained.referenced_tables(tables, case["left"], case["right"])
    drops = constrained.Drops.parse(case.get("drop"))
    extra = harvest(case["left"], case["right"], {n: tables[n].columns for n in names}) if use_harvest else None
    setups = [constrained.database(tables, names, seed, drops, extra=extra) for seed in range(databases)]
    witness = case.get("witness")
    if isinstance(witness, dict):
        setups.append(constrained.witness_database(tables, names, witness, drops))
    return setups


def run_case(client, case: dict, databases: int, use_harvest: bool = True, specials: bool = False) -> dict:
    label = case["label"]
    if label not in ("equivalent", "not_equivalent", "unresolved"):
        return {"id": case["id"], "label": label, "outcome": "unscored"}
    static = prove_static(case["left"], case["right"]) is Verdict.PROVEN
    if "database" in case:
        setups = constrained_setups(case, databases, use_harvest)
    elif case.get("family"):
        # Unsafe-rewrite pairs: two INT64 tables over the small domain 0..3 with duplicates, up to 6 rows
        # each. Even seeds add NULL to the domain (about one cell in five, which INTERSECT and NOT IN
        # need), odd seeds do not (a NULL key never joins, which hides duplicate-producing joins).
        setups = []
        for seed in range(databases):
            domain = {c: [0, 1, 2, 3, None] if seed % 2 == 0 else [0, 1, 2, 3] for c in "abc"}
            setups.append(random_database(UNSAFE_SCHEMA, seed, max_rows=6, extra=domain))
    else:
        schema = schema_of(case)
        extra = harvest(case["left"], case["right"], schema) if use_harvest else None
        setups = [random_database(schema, seed, extra=extra, specials=specials) for seed in range(databases)]
        witness = case.get("witness")
        if witness:
            setups.append(fixed_database(schema, witness["table"], witness["columns"], witness["rows"]))
    refuted_on, errors, agreed = None, collections.Counter(), 0
    for setup in setups:
        result = compare(client, case["left"], case["right"], setup=setup)
        if result.verdict is Verdict.REFUTED:
            refuted_on = {"setup": setup, "left": result.left_rows[:5], "right": result.right_rows[:5]}
            break
        if "emulator" in result.reason:
            errors[classify_error(result.reason)] += 1
        else:
            agreed += 1
    if label == "equivalent":
        outcome = "WRONG" if refuted_on else ("emulator_error" if not agreed else "ok")
    elif label == "unresolved":
        outcome = "refuted" if refuted_on else ("emulator_error" if not agreed else "not_refuted")
    else:
        # "incomplete": some database failed on the emulator, so the miss is not clean
        outcome = "caught" if refuted_on else ("emulator_error" if not agreed else ("incomplete" if errors else "missed"))
    if static and label == "equivalent" and not refuted_on:
        outcome = "ok"
    return {
        "id": case["id"], "label": label, "outcome": outcome, "static_proof": static,
        "databases_agreed": agreed, "errors": dict(errors), "counterexample": refuted_on,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", help="a name under benchmarks/pairs/, e.g. documented-rewrites")
    parser.add_argument("--databases", type=int, default=20, help="random databases per case")
    parser.add_argument("--json", help="write per-case outcomes to this file")
    parser.add_argument("--no-harvest", action="store_true", help="seed data from the base pools only (the baseline)")
    parser.add_argument("--specials", action="store_true", help="add NaN, infinities, -0.0 and scale-limit NUMERIC values to the pools")
    parser.add_argument("--only", help="run only case ids containing this text")
    parser.add_argument("--shard", help="i/n: run every n-th case starting at i, so n runs can share one corpus")
    args = parser.parse_args()
    cases = [json.loads(line) for line in (ROOT / "benchmarks/pairs" / f"{args.corpus}.jsonl").read_text().splitlines() if line]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.shard:
        index, count = (int(part) for part in args.shard.split("/"))
        cases = cases[index::count]
    outcomes = []
    # The emulator slows down as datasets accumulate (every query sees every table),
    # so it is restarted every RECYCLE cases.
    for start in range(0, len(cases), RECYCLE):
        with running_emulator() as client:
            for case in cases[start : start + RECYCLE]:
                outcome = run_case(client, case, args.databases, use_harvest=not args.no_harvest, specials=args.specials)
                outcomes.append(outcome)
                note = next(iter(outcome.get("errors", {})), "")
                print(f"{outcome['outcome']:15} {outcome['label']:15} {outcome['id']:32} {note[:70]}", flush=True)
    counts = collections.Counter((o["label"], o["outcome"]) for o in outcomes)
    print("\n" + ", ".join(f"{label}/{outcome}: {n}" for (label, outcome), n in sorted(counts.items())))
    gaps = collections.Counter()
    for o in outcomes:
        for message, n in o.get("errors", {}).items():
            gaps[message] += 1
    for message, n in gaps.most_common(15):
        print(f"  gap in {n:3} cases: {message}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"counts": {f"{a}/{b}": n for (a, b), n in counts.items()}, "gaps": dict(gaps), "cases": outcomes}, indent=1, default=str))
    return 1 if any(o["outcome"] == "WRONG" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
