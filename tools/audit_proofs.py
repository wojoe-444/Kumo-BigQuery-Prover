#!/usr/bin/env python3
"""Proof audit: every pair the static layers prove is run on the emulator's seeded databases.

Input: a JSONL file of pairs with optional ``schema`` (``{table: [columns]}`` or ``{table: {column: type}}``) and
``types``, such as benchmarks/audit/upstream-test-pairs.jsonl (every pair that Kumo_SQL_Prover's own tests hand to its
provers, recorded with tools/record_prover_calls.py). There are no labels. The static layers run on each pair first;
only a proved pair is run on the emulator, and an emulator refutation of a proved pair is ``PROOF_REFUTED``: a false
proof until a person shows it is an emulator bug or a bad assumption.

Outcomes: proved_checked (proved, agreed on at least one database), proved_unchecked (proved, but the emulator could not
run the pair: unknown column types, a function it lacks), proved_volatile (proved with a volatile call left identical on
both sides, which two runs cannot confirm), emulator_bug (refuted, but real BigQuery agrees: benchmarks/audit/known-emulator-bugs.json),
ASSUMPTION_REFUTED (refuted, and the proof lists a modelling assumption: no NaN, SUM independent of row order), PROOF_REFUTED, not_proved. The script exits 1 on PROOF_REFUTED.

    python tools/audit_proofs.py benchmarks/audit/upstream-test-pairs.jsonl --databases 8 --workers 4 --json out.json
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

RECYCLE = 6

#: Assumptions that are modelling choices: a proof that lists one and is refuted is refuted *under* it, which is the
#: open FLOAT64 SUM question and the no-NaN assumption, not a defect the audit can attribute to a rule.
MODEL_ASSUMPTIONS = ("FLOAT64 values are never NaN", "SUM and AVG are treated as independent of row order")
KNOWN_BUGS = ROOT / "benchmarks" / "audit" / "known-emulator-bugs.json"


def declared_schema(pair: dict):
    """What the pair declared: the typed mapping if it has one, else the column lists, else nothing."""

    if pair.get("types"):
        return pair["types"]
    return pair.get("schema") or None


def emulator_schema(pair: dict, infer) -> dict[str, dict[str, str]]:
    """Types for building data. A table the pair declared keeps exactly its declared columns, in the declared order
    (``SELECT *`` depends on it, and a column the declaration lacks changes how a bare name resolves); a table it did not
    declare comes from a guess. Types the pair did not give are guessed, else INT64."""

    guessed = infer([pair["left"], pair["right"]])
    declared_types = {t.lower(): {c.lower(): k for c, k in cols.items()} for t, cols in (pair.get("types") or {}).items()}
    declared_columns = {t.lower(): [c.lower() for c in cols] for t, cols in (pair.get("schema") or {}).items()}
    merged: dict[str, dict[str, str]] = {}
    for table in dict.fromkeys([*declared_columns, *declared_types, *guessed]):
        if table in declared_columns or table in declared_types:
            order = declared_columns.get(table) or list(declared_types[table])
            merged[table] = {c: declared_types.get(table, {}).get(c) or guessed.get(table, {}).get(c) or "INT64" for c in order}
        else:
            merged[table] = dict(guessed[table])
    return {table: cols for table, cols in merged.items() if cols}


def work(chunk: list[dict], databases: int) -> list[dict]:
    from kumo_bq_prover.datagen import random_database
    from kumo_bq_prover.emulator import running_emulator
    from kumo_bq_prover.harvest import harvest
    from kumo_bq_prover.infer import infer_schema
    from kumo_bq_prover.oracle import compare
    from kumo_bq_prover.ported import volatile_calls
    from kumo_bq_prover.static import prove_static_detailed
    from kumo_bq_prover.verdict import Verdict

    results, pending = [], []
    known_bugs = json.loads(KNOWN_BUGS.read_text()) if KNOWN_BUGS.exists() else {}
    for pair in chunk:
        try:
            static = prove_static_detailed(pair["left"], pair["right"], declared_schema(pair))
        except Exception as error:  # noqa: BLE001 - recorded, never silent
            results.append({"id": pair["id"], "outcome": "static_error", "detail": repr(error)[:200]})
            continue
        if static.verdict is not Verdict.PROVEN:
            results.append({"id": pair["id"], "outcome": "not_proved"})
        elif any(volatile_calls.read(sql, "bigquery")[0] for sql in (pair["left"], pair["right"])):
            # A volatile call left identical on both sides is proved (the same call, in place); two runs of it differ.
            results.append({"id": pair["id"], "outcome": "proved_volatile", "layer": static.layer})
        else:
            pending.append((pair, static))
    for start in range(0, len(pending), RECYCLE):
        with running_emulator() as client:
            for pair, static in pending[start : start + RECYCLE]:
                schema = emulator_schema(pair, infer_schema)
                extra = harvest(pair["left"], pair["right"], schema) if schema else None
                agreed, errors, refuted = 0, collections.Counter(), None
                for seed in range(databases):
                    if not schema:
                        break
                    result = compare(client, pair["left"], pair["right"], setup=random_database(schema, seed, extra=extra))
                    if result.verdict is Verdict.REFUTED:
                        refuted = {"setup": result.setup, "left": result.left_rows[:5], "right": result.right_rows[:5]}
                        break
                    if "emulator" in result.reason:
                        errors[result.reason[:140]] += 1
                    else:
                        agreed += 1
                outcome = "PROOF_REFUTED" if refuted else ("proved_checked" if agreed else "proved_unchecked")
                if refuted and pair["id"] in known_bugs:
                    outcome = "emulator_bug"  # checked on real BigQuery: both queries agree there (see KNOWN_BUGS)
                elif refuted and any(a.startswith(m) for a in static.assumptions for m in MODEL_ASSUMPTIONS):
                    outcome = "ASSUMPTION_REFUTED"
                results.append({
                    "id": pair["id"], "outcome": outcome, "layer": static.layer, "assumptions": list(static.assumptions),
                    "databases_agreed": agreed, "errors": dict(errors), "counterexample": refuted,
                })
                print(f"{outcome:16} {pair['id']}", flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pairs", help="JSONL file of pairs")
    parser.add_argument("--databases", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="only the first N pairs")
    parser.add_argument("--json", help="write per-pair outcomes here")
    args = parser.parse_args()
    pairs = [json.loads(line) for line in pathlib.Path(args.pairs).read_text().splitlines() if line]
    if args.limit:
        pairs = pairs[: args.limit]
    chunks = [pairs[i :: args.workers * 8] for i in range(args.workers * 8)]
    results: list[dict] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for part in pool.map(work, [c for c in chunks if c], [args.databases] * len([c for c in chunks if c])):
            results.extend(part)
    counts = collections.Counter(r["outcome"] for r in results)
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    layers = collections.Counter(r.get("layer") for r in results if r["outcome"].startswith("proved") or r["outcome"] == "PROOF_REFUTED")
    print("layers of the proofs:", dict(layers))
    gaps = collections.Counter(m for r in results for m in r.get("errors", {}))
    for message, n in gaps.most_common(10):
        print(f"  emulator could not run, {n:3} pairs: {message}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"counts": dict(counts), "cases": results}, indent=1, default=str))
    return 1 if counts["PROOF_REFUTED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
