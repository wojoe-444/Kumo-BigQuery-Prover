#!/usr/bin/env python3
"""Run Google's GoogleSQL compliance cases (the 4,748 claimed queries) on the BigQuery emulator.

    python tools/run_compliance.py                    # development files, summary
    python tools/run_compliance.py --split heldout    # held-out files (measured once; see the docs page)
    python tools/run_compliance.py --split all --json results.json --jobs 4
    python tools/run_compliance.py --file 'string_*'  # one test file (a name or prefix*), repeatable

The emulator is restarted every few files (it slows down as datasets accumulate). Exit status is 0
whatever the result: the run measures the emulator, it does not gate a change.
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import json
import multiprocessing
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover import gsql_compliance as g  # noqa: E402
from kumo_bq_prover.emulator import running_emulator  # noqa: E402

RESTART_EVERY = 4  # files per emulator process


def work(chunk: list[tuple[str, list, list]]) -> list[dict]:
    out = []
    for file, file_cases, prepare in chunk:
        started, remaining = time.time(), file_cases
        while remaining:
            with running_emulator(max_rss_mb=2500) as client:
                client.timeout = 30.0
                outcomes, remaining = g.run_file(client, remaining, prepare)
            out.extend(o.__dict__ for o in outcomes)
        print(f"  {file}: {len(file_cases)} cases in {time.time() - started:.0f}s", flush=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev", choices=["dev", "heldout", "all"])
    parser.add_argument("--file", action="append", default=[], help="a test file name or prefix* (repeatable)")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--show", type=int, default=0, help="print this many wrong results in full")
    args = parser.parse_args()
    splits = ["dev", "heldout"] if args.split == "all" else [args.split]
    by_file: dict[str, tuple[list, list]] = {}
    for split in splits:
        entries = {e["file"]: e["prepare"] for e in g.load_files(split)}
        for case in g.cases(split):
            if not case.claimed:
                continue
            if args.file and not any(fnmatch.fnmatch(case.file, pat if "*" in pat else pat + "*") for pat in args.file):
                continue
            by_file.setdefault(case.file, ([], entries[case.file]))[0].append(case)
    files = sorted(by_file, key=lambda f: -len(by_file[f][0]))
    chunks = [[(f, by_file[f][0], by_file[f][1]) for f in files[i:i + RESTART_EVERY]] for i in range(0, len(files), RESTART_EVERY)]
    print(f"{sum(len(by_file[f][0]) for f in files)} claimed cases in {len(files)} files, {len(chunks)} emulator runs", flush=True)
    started = time.time()
    with multiprocessing.Pool(args.jobs) as pool:
        outcomes = [o for part in pool.imap_unordered(work, chunks) for o in part]
    seconds = time.time() - started
    counts = collections.Counter(o["verdict"] for o in outcomes)
    print(f"\n{len(outcomes)} cases in {seconds:.0f}s: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    for verdict in ("wrong", "missing_error", "emulator_error", "crash", "fixture", "zone", "unscored"):
        reasons = collections.Counter(o["reason"][:90] for o in outcomes if o["verdict"] == verdict)
        for reason, n in reasons.most_common(12):
            print(f"  {verdict:15} {n:4}  {reason}")
    for o in [o for o in outcomes if o["verdict"] == "wrong"][: args.show]:
        print(f"\n{o['file']}/{o['name']}\n  {o['detail'][:600]}")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({"source": g.SOURCE, "counts": counts, "outcomes": outcomes}, indent=0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
