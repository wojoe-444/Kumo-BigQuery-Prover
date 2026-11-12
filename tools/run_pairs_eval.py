#!/usr/bin/env python3
"""Score the emulator oracle on a labelled pair corpus in benchmarks/pairs/.

For every case the two queries run on N seeded random databases (plus the
case's own witness rows when it has them). Outcomes:

  equivalent label      proved | ok | WRONG (refuted: an emulator bug or a bad label; a refutation the guard demotes is ok) | emulator_error
                        PROOF_REFUTED (proved by a static layer and refuted: a false proof until shown otherwise)
  not_equivalent label  caught (refuted) | demoted (refuted on the emulator, but on a construct or value it is not
                        trusted on, see trust.py: reported UNKNOWN) | missed | emulator_error | FALSE_PROOF (a static layer proved it)
  either label          assumed (proved, but the proof lists an assumption the case names in violates/discharged: counted as unknown)
  unresolved label      refuted | demoted | not_refuted | emulator_error | FALSE_PROOF (a static layer proved a pair
                        the earlier prover refuted); the run decides the label (the unsafe-rewrite corpus)
  any other label       unscored

A case with a "database" field (the sample-database corpus) runs on databases generated under the declared keys of
benchmarks/pairs/schemas/<database>.sql, minus the case's "drop" list, plus its own witness rows completed with legal
values (see constrained.py). A not_equivalent case the emulator does not refute but failed on some databases is
"incomplete", not "missed".

Every proved pair also runs on the databases: the emulator is the false-proof detector. The script exits 1 when
any case is WRONG, PROOF_REFUTED or FALSE_PROOF. Emulator rejections are grouped by
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
from kumo_bq_prover.static import LAYERS, prove_static_detailed  # noqa: E402
from kumo_bq_prover.verdict import Verdict  # noqa: E402

RECYCLE = 6

NUMERIC_TRAPS_SCHEMA = {
    "t": {"x": "INT64", "y": "INT64", "f": "FLOAT64", "g": "FLOAT64", "n": "NUMERIC", "m": "NUMERIC", "s": "STRING", "c": "BOOL"}
}


UNSAFE_SCHEMA = {"t": {c: "INT64" for c in "abc"}, "u": {c: "INT64" for c in "abc"}}
_DDL: dict[str, dict] = {}


def tables_of(database: str) -> dict:
    if database not in _DDL:
        _DDL[database] = constrained.parse_ddl((ROOT / "benchmarks/pairs/schemas" / f"{database}.sql").read_text())
    return _DDL[database]


def schema_of(case: dict) -> dict:
    if "database" in case:
        tables = tables_of(case["database"])
        names = constrained.referenced_tables(tables, case["left"], case["right"])
        return {name: dict(tables[name].columns) for name in names}
    if case.get("family"):
        return UNSAFE_SCHEMA
    return case.get("schema") or NUMERIC_TRAPS_SCHEMA


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


def unsafe_setups(databases: int) -> list[list[str]]:
    """Two INT64 tables over the small domain 0..3 with duplicates, up to 6 rows each. Even seeds add NULL to the
    domain (about one cell in five, which INTERSECT and NOT IN need); odd seeds do not (a NULL key never joins,
    which hides duplicate-producing joins)."""
    setups = []
    for seed in range(databases):
        domain = {c: [0, 1, 2, 3, None] if seed % 2 == 0 else [0, 1, 2, 3] for c in "abc"}
        setups.append(random_database(UNSAFE_SCHEMA, seed, max_rows=6, extra=domain))
    return setups


def classify_error(reason: str) -> str:
    text = re.sub(r"\s+", " ", reason)
    match = re.search(r"no such function: (\w+)", text)
    if match:
        return f"no such function: {match.group(1)}"
    message = re.search(r'"message":"([^"]{0,160})', text)
    return (message.group(1) if message else text[:160]).replace("\\", "")


def run_case(client, case: dict, databases: int, use_harvest: bool = True, specials: bool = False, layers: tuple[str, ...] = LAYERS) -> dict:
    schema = schema_of(case)
    label = case["label"]
    if label not in ("equivalent", "not_equivalent", "unresolved"):
        return {"id": case["id"], "label": label, "outcome": "unscored"}
    static = prove_static_detailed(case["left"], case["right"], schema_of(case), layers=layers)
    if "database" in case:
        setups = constrained_setups(case, databases, use_harvest)
    elif case.get("family"):
        setups = unsafe_setups(databases)
    else:
        extra = harvest(case["left"], case["right"], schema) if use_harvest else None
        setups = [random_database(schema, seed, extra=extra, specials=specials, extremes=seed % 2 == 1) for seed in range(databases)]
        witness = case.get("witness")
        if witness:
            setups.append(fixed_database(schema, witness["table"], witness["columns"], witness["rows"]))
    refuted_on, errors, agreed, demoted = None, collections.Counter(), 0, None
    # A proved pair runs without the trust guard: any difference the emulator finds on it must surface (false-proof detector).
    guarded = static.verdict is not Verdict.PROVEN
    for setup in setups:
        result = compare(client, case["left"], case["right"], setup=setup, trust=guarded)
        if result.verdict is Verdict.REFUTED:
            refuted_on = {"setup": setup, "left": result.left_rows[:5], "right": result.right_rows[:5]}
            break
        if result.untrusted:  # different rows, but on a construct or value the emulator is not trusted on (see trust.py)
            demoted = demoted or {"setup": setup, "left": result.left_rows[:5], "right": result.right_rows[:5], "untrusted": result.untrusted}
            continue
        if "emulator" in result.reason:
            errors[classify_error(result.reason)] += 1
        else:
            agreed += 1
    proved = static.verdict is Verdict.PROVEN
    # A proof that lists an assumption the case violates (or says does not apply) is disclosed, not clean: it is not
    # counted as a proof and is not a false proof. The corpus names those assumptions in "violates" / "discharged".
    named = list(case.get("violates") or []) + list(case.get("discharged") or [])
    assumed = proved and any(a.startswith(n) for a in static.assumptions for n in named)
    if label == "equivalent":
        if assumed:
            outcome = "PROOF_REFUTED" if refuted_on else "assumed"
        elif proved:
            outcome = "PROOF_REFUTED" if refuted_on else "proved"
        else:
            outcome = "WRONG" if refuted_on else ("ok" if (agreed or demoted) else "emulator_error")
    elif label == "unresolved":
        if proved:
            outcome = "FALSE_PROOF"
        else:
            outcome = "refuted" if refuted_on else ("demoted" if demoted else ("emulator_error" if not agreed else "not_refuted"))
    else:
        if assumed:
            outcome = "assumed"
        elif proved:
            outcome = "FALSE_PROOF"
        else:
            outcome = "caught" if refuted_on else ("demoted" if demoted else ("emulator_error" if not agreed else ("incomplete" if errors else "missed")))
    return {
        "id": case["id"], "label": label, "outcome": outcome, "proof_layer": static.layer if proved else None, "assumptions": list(static.assumptions), "error_report": static.errors, "withheld": static.withheld or None,
        "databases_agreed": agreed, "errors": dict(errors), "counterexample": refuted_on, "demoted": demoted,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", help="a name under benchmarks/pairs/, e.g. documented-rewrites")
    parser.add_argument("--databases", type=int, default=20, help="random databases per case")
    parser.add_argument("--json", help="write per-case outcomes to this file")
    parser.add_argument("--no-harvest", action="store_true", help="seed data from the base pools only (the baseline)")
    parser.add_argument("--specials", action="store_true", help="add NaN, infinities, -0.0 and scale-limit NUMERIC values to the pools")
    parser.add_argument("--only", help="run only case ids containing this text")
    parser.add_argument("--ids", help="a file with one case id per line: run only those cases")
    parser.add_argument("--shard", help="i/n: run every n-th case starting at i, so n runs can share one corpus")
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all", help="held-out is every fourth case of the file, from the fourth (index %% 4 == 3, the convention of KumoSQL's numeric-traps bench); dev is the rest")
    parser.add_argument("--layers", default=",".join(LAYERS), help="comma-separated static layers to use (identical,structural,algebraic,smt); 'none' for the oracle alone")
    args = parser.parse_args()
    layers = tuple(x for x in args.layers.split(",") if x and x != "none")
    cases = [json.loads(line) for line in (ROOT / "benchmarks/pairs" / f"{args.corpus}.jsonl").read_text().splitlines() if line]
    if args.split != "all":
        cases = [c for i, c in enumerate(cases) if (i % 4 == 3) == (args.split == "held-out")]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.ids:
        wanted = set(pathlib.Path(args.ids).read_text().split())
        cases = [c for c in cases if c["id"] in wanted]
    if args.shard:
        index, count = (int(part) for part in args.shard.split("/"))
        cases = cases[index::count]
    outcomes = []
    # The emulator slows down as datasets accumulate (every query sees every table),
    # so it is restarted every RECYCLE cases.
    for start in range(0, len(cases), RECYCLE):
        with running_emulator() as client:
            for case in cases[start : start + RECYCLE]:
                outcome = run_case(client, case, args.databases, use_harvest=not args.no_harvest, specials=args.specials, layers=layers)
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
    return 1 if any(o["outcome"] in ("WRONG", "PROOF_REFUTED", "FALSE_PROOF") for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
