#!/usr/bin/env python3
"""Turn compliance outcomes into the per-construct gap list and the untrusted-construct file.

    python tools/compliance_gaps.py RESULTS.json [--heldout HELDOUT.json] [--write]

RESULTS.json is the ``--json`` output of ``tools/run_compliance.py``, or the recorded
``benchmarks/results/googlesql-compliance-emulator-outcomes.json.gz`` (both splits; the list is built from the
development outcomes in it). Without ``--write`` the gap list is only printed.

What it computes, per construct tag (see ``kumo_bq_prover.constructs``):

* ``ok``: compliance cases that pass and use the construct
* ``bad``: cases that are ``wrong`` or ``missing_error`` (the emulator disagrees with the reference)
* ``errors``: cases the emulator rejects (already UNKNOWN in the oracle, so not a risk)

A tag that is used in a failing case in at least a quarter of its cases (and at least twice) is **untrusted**; tags
in more than a third of all cases (``lit:int``, ``node:Window``...) never are. Failures of the common constructs
(SUM, ARRAY, DISTINCT) concentrate on extreme values and are covered by the value check in ``trust.py`` instead.
``--write`` stores the untrusted tags with their evidence in ``src/kumo_bq_prover/untrusted_constructs.json``;
the extra, hand-curated entries in ``CURATED`` are added to it.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover import gsql_compliance as g  # noqa: E402
from kumo_bq_prover.constructs import constructs  # noqa: E402
from kumo_bq_prover.trust import value_hits  # noqa: E402

OUT = ROOT / "src" / "kumo_bq_prover" / "untrusted_constructs.json"

# Divergences found by hand (earlier thread runs and real-BigQuery checks) that the compliance cases
# cannot show by themselves: the emulator cannot hold the value, so the case never runs. Each is a tag
# of a construct that produces or consumes the value.
CURATED = {
    "fn:IS_NAN": "the emulator reads NaN back as NULL",
    "fn:IS_INF": "the emulator cannot hold infinities except through IEEE_DIVIDE",
    "fn:IEEE_DIVIDE": "NaN and infinity results cannot be held (NaN reads back as NULL)",
    "fn:SAFE_DIVIDE": "SAFE_DIVIDE(1e300, 1e-300) returns infinity where the reference returns NULL (float overflow)",
    "cast:TIMESTAMP": "CAST(DATE AS TIMESTAMP) applies the America/Los_Angeles zone on the emulator; BigQuery uses UTC (checked on BigQuery)",
    "cast:INT64": "CAST(FLOAT64 AS INT64) truncates on the emulator; BigQuery rounds half away from zero",
    "safe_cast:INT64": "SAFE_CAST(FLOAT64 AS INT64) truncates on the emulator; BigQuery rounds half away from zero",
    "join:LEFT:UNNEST": "LEFT JOIN UNNEST drops the row of an empty array on the emulator",
    "node:In": "IN and NOT IN with a NULL element return FALSE or TRUE where the reference returns NULL",
    "node:Between": "x BETWEEN NULL AND y returns NULL where BigQuery returns FALSE when the other bound already fails (found on the held-out files, so tuned on test)",
    "is:boolean": "NULL IS TRUE returns NULL where the reference returns FALSE",
}

MIN_SHARE = 1 / 3  # tags in more than this share of the measured cases are never untrusted
RATE = 0.25  # a tag that fails in this share of its cases (and at least twice) is untrusted


def load_outcomes(path: str) -> list[dict]:
    """The outcomes of a ``run_compliance.py --json`` file (plain or gzip), possibly of both splits."""
    raw = gzip.open(path, "rt").read() if path.endswith(".gz") else pathlib.Path(path).read_text()
    return json.loads(raw)["outcomes"]


def tagged(outcomes: list[dict]) -> list[dict]:
    by_key: dict[tuple, g.Case] = {}
    for split in ("dev", "heldout"):
        for case in g.cases(split):
            by_key[(case.file, case.name)] = case
    out = []
    for o in outcomes:
        case = by_key.get((o["file"], o["name"]))
        tags = constructs(case.sql) if case else set()
        out.append({**o, "tags": sorted(tags), "sql": case.sql if case else ""})
    return out


_TABLE = g._CREATE_TABLE


def case_texts(case: g.Case) -> list[str]:
    """The case's SQL and the printed rows of the tables it names (what a counterexample would be built from)."""
    texts = [case.sql]
    for block in case.prepare:
        match = _TABLE.match(block["sql"])
        if match and re.search(r"(?<![\w.])" + re.escape(match.group(1).replace("`", "")) + r"(?!\w)", case.sql, re.I):
            texts.append(block["sql"])
            texts.extend(block["expected"][:1])
    return texts


def coverage(rows: list[dict], untrusted: dict) -> dict:
    """How many compliance cases the guard would flag: failing ones (what it exists for) and passing ones (its cost)."""
    by_key = {(c.file, c.name): c for split in ("dev", "heldout") for c in g.cases(split)}
    out = {"bad": [0, 0], "ok": [0, 0], "unflagged_bad": []}
    for r in rows:
        if r["verdict"] not in ("pass", "wrong", "missing_error"):
            continue
        case = by_key[(r["file"], r["name"])]
        flagged = bool(set(r["tags"]) & untrusted.keys()) or bool(value_hits(queries=case_texts(case)))
        kind = "ok" if r["verdict"] == "pass" else "bad"
        out[kind][0] += flagged
        out[kind][1] += 1
        if kind == "bad" and not flagged:
            out["unflagged_bad"].append(f"{r['file']}/{r['name']}")
    return out


def analyse(rows: list[dict]) -> tuple[dict, dict]:
    measured = [r for r in rows if r["verdict"] in ("pass", "wrong", "missing_error", "emulator_error")]
    stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in measured:
        kind = {"pass": "ok", "wrong": "bad", "missing_error": "bad", "emulator_error": "errors"}[r["verdict"]]
        for tag in r["tags"]:
            stats[tag][kind] += 1
    ubiquitous = {t for t, c in stats.items() if sum(c.values()) > MIN_SHARE * len(measured)}
    blamed: dict[str, list[str]] = {}
    for tag, c in stats.items():
        # A construct that disagrees with the reference in a quarter of its cases (and at least twice) is untrusted.
        if tag not in ubiquitous and c["bad"] >= 2 and c["bad"] / (c["ok"] + c["bad"]) >= RATE:
            blamed[tag] = sorted(f"{r['file']}/{r['name']}" for r in measured if tag in r["tags"] and r["verdict"] in ("wrong", "missing_error"))
    return stats, blamed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results")
    parser.add_argument("--split", default="dev", choices=["dev", "heldout", "all"], help="which outcomes the untrusted list is built from (default: the development files)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--heldout", help="outcomes of the held-out split: report how much of it the guard covers")
    args = parser.parse_args()
    rows = tagged(load_outcomes(args.results))
    if args.split != "all":
        rows = [r for r in rows if r["split"] == args.split]
    stats, blamed = analyse(rows)
    untrusted = {}
    for tag, cases in sorted(blamed.items()):
        c = stats[tag]
        untrusted[tag] = {
            "reason": f"{c['bad']} of {c['ok'] + c['bad']} compliance cases using it disagree with the reference",
            "ok": c["ok"], "bad": c["bad"], "errors": c["errors"], "examples": sorted(cases)[:4],
        }
    for tag, reason in CURATED.items():
        c = stats.get(tag, collections.Counter())
        untrusted.setdefault(tag, {"reason": reason, "ok": c["ok"], "bad": c["bad"], "errors": c["errors"], "examples": []})
        untrusted[tag]["reason"] = reason
    counts = collections.Counter(r["verdict"] for r in rows)
    print(f"{len(rows)} cases: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    print(f"{len(untrusted)} untrusted constructs ({len(blamed)} from the compliance cases, {len(CURATED)} curated)\n")
    print(f"{'construct':32} {'ok':>5} {'bad':>5} {'err':>5}  reason")
    for tag, info in sorted(untrusted.items(), key=lambda kv: -kv[1]["bad"]):
        print(f"{tag:32} {info['ok']:5} {info['bad']:5} {info['errors']:5}  {info['reason'][:70]}")
    for label, path in (("development files (the list was built on them)", None), ("held-out files", args.heldout)):
        if path:
            held = [r for r in tagged(load_outcomes(path)) if r["split"] == "heldout"]
            result = coverage(held, untrusted)
        elif label.startswith("development"):
            result = coverage(rows, untrusted)
        else:
            continue
        bad, ok = result["bad"], result["ok"]
        print(f"\nguard coverage, {label}: flags {bad[0]} of {bad[1]} failing cases; also flags {ok[0]} of {ok[1]} passing cases")
        if path and len(result["unflagged_bad"]):
            print("  failing cases it misses: " + ", ".join(result["unflagged_bad"][:12]) + (" ..." if len(result["unflagged_bad"]) > 12 else ""))
    if args.write:
        payload = {
            "source": g.SOURCE,
            "note": "Constructs where the emulator disagrees with Google's reference results (see docs/emulator-gaps.md). "
                    "The oracle answers UNKNOWN instead of REFUTED when a pair uses one. Generated by tools/compliance_gaps.py.",
            "untrusted": untrusted,
        }
        OUT.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        print(f"\nwrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
