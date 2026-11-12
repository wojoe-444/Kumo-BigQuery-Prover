#!/usr/bin/env python3
"""Generate Spider 2.0 BigQuery pairs and score the emulator oracle on them.

    python tools/spider2_pairs.py --split dev --workers 4
    python tools/spider2_pairs.py --split held-out --workers 4      # run once, at the end

Writes ``benchmarks/pairs/spider2-bigquery-<split>.jsonl`` (one labelled pair per line)
and ``benchmarks/results/spider2-pairs-<split>.json`` (counts, and the reason each
query that does not run was left out). Exits 1 when a rewrite is refuted (WRONG) or
a refuted pair is statically proved (FALSE_PROOF); look at each one by hand.
"""

from __future__ import annotations

import argparse
import collections
import json
import multiprocessing
import pathlib
import signal
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover import spider2  # noqa: E402
from kumo_bq_prover.emulator import running_emulator  # noqa: E402

QUERY_DEADLINE = 600  # seconds for one query, emulator start to finish


class _Deadline(BaseException):
    pass


def _alarm(*_):
    raise _Deadline()


def work(task: tuple) -> dict:
    case_id, databases, max_mutations, filtered_share = task
    started = time.time()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(QUERY_DEADLINE)
    try:
        with running_emulator() as client:
            run = spider2.run_query(client, case_id, databases=databases, max_mutations=max_mutations, filtered_share=filtered_share)
    except _Deadline:
        run = spider2.QueryRun(case_id, "timeout", f"over {QUERY_DEADLINE} s")
    except Exception as error:  # noqa: BLE001
        run = spider2.QueryRun(case_id, "crash", f"{type(error).__name__}: {str(error)[:120]}")
    finally:
        signal.alarm(0)
    return {
        "case_id": run.case_id, "status": run.status, "reason": run.reason, "databases": run.databases,
        "databases_nonempty": run.databases_nonempty, "databases_nonempty_filtered": run.databases_nonempty_filtered, "builder": run.builder, "databases_failed": run.databases_failed, "base_error": run.base_error, "pairs": run.pairs, "seconds": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "held-out", "all"], default="dev")
    parser.add_argument("--ids", nargs="*", help="only these case ids")
    parser.add_argument("--databases", type=int, default=20, help="random databases per query")
    parser.add_argument("--max-mutations", type=int, default=8)
    parser.add_argument("--filtered-share", type=float, default=0.75, help="share of the databases built to satisfy the query's own filters (the rest are plain random)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-write", action="store_true", help="print the summary, write nothing")
    args = parser.parse_args()
    cases = [c["id"] for c in spider2.manifest() if args.split == "all" or spider2.split_of(c["id"]) == args.split]
    if args.ids:
        cases = [c for c in cases if c in args.ids]
    tasks = [(c, args.databases, args.max_mutations, args.filtered_share) for c in cases]
    with multiprocessing.Pool(args.workers) as pool:
        results = []
        for result in pool.imap_unordered(work, tasks):
            results.append(result)
            print(f"{result['case_id']:8} {result['status']:15} pairs={len(result['pairs']):3} {result['seconds']:6.1f}s {result['reason'][:70]}", flush=True)
    results.sort(key=lambda r: r["case_id"])
    pairs = [p for r in results for p in r["pairs"]]
    status = collections.Counter(r["status"] for r in results)
    reasons = collections.Counter(f"{r['status']}: {r['reason']}" for r in results if r["status"] != "ok")
    by_kind = collections.Counter((p["kind"], p["oracle"]) for p in pairs)
    wrong = [p["id"] for p in pairs if p["oracle"] == "WRONG"]
    false_proofs = [p["id"] for p in pairs if p["oracle"] == "FALSE_PROOF"]
    static_rewrites = collections.Counter(p["static"]["verdict"] for p in pairs if p["kind"] == "rewrite")
    vacuous = sum(1 for r in results if r["status"] == "ok" and r["databases_nonempty"] == 0)
    summary = {
        "split": args.split,
        "queries": len(results),
        "queries_by_status": dict(status),
        "queries_left_empty_on_every_database": vacuous,
        "pairs": len(pairs),
        "pairs_by_kind_and_oracle": {f"{k}/{o}": n for (k, o), n in sorted(by_kind.items())},
        "labels": dict(collections.Counter(p["label"] for p in pairs)),
        "rewrite_refuted": wrong,
        "false_proofs": false_proofs,
        "static_verdict_on_rewrites": dict(static_rewrites),
        "static_proved_by_layer": dict(collections.Counter(p["static"]["layer"] for p in pairs if p["static_proof"])),
        "databases_per_query": args.databases,
        "does_not_run": dict(reasons.most_common()),
    }
    print(json.dumps(summary, indent=1))
    if not args.no_write:
        out = ROOT / "benchmarks" / "pairs" / f"spider2-bigquery-{args.split}.jsonl"
        out.write_text("".join(json.dumps(p, sort_keys=True) + "\n" for p in pairs))
        (ROOT / "benchmarks" / "results" / f"spider2-pairs-{args.split}.json").write_text(json.dumps({**summary, "queries_detail": [
            {k: r[k] for k in ("case_id", "status", "reason", "databases_nonempty", "databases_nonempty_filtered", "databases_failed", "base_error", "builder", "seconds")} for r in results]}, indent=1))
    return 1 if wrong or false_proofs else 0


if __name__ == "__main__":
    raise SystemExit(main())
