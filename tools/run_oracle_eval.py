#!/usr/bin/env python3
"""Score the emulator oracle on benchmarks/bq_shapes/cases.json (refutation only).

Per case: a not_equivalent pair is *caught* when the oracle refutes it. An
equivalent pair the oracle refutes is WRONG (an emulator bug or a bad label) and
makes this script exit 1. Emulator rejections are reported apart, never as wins.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumo_bq_prover.emulator import running_emulator  # noqa: E402
from kumo_bq_prover.oracle import compare  # noqa: E402
from kumo_bq_prover.verdict import Verdict  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "benchmarks/bq_shapes/cases.json"))
    parser.add_argument("--json", help="write per-case outcomes to this file")
    args = parser.parse_args()
    corpus = json.loads(pathlib.Path(args.cases).read_text())
    outcomes = []
    with running_emulator() as client:
        for case in corpus["cases"]:
            setup = [corpus["tables"][name] for name in case["tables"]]
            result = compare(client, case["left"], case["right"], setup=setup, ordered=case["ordered"])
            rejected = result.verdict is Verdict.UNKNOWN and "emulator" in result.reason
            if case["label"] == "equivalent":
                outcome = "WRONG" if result.verdict is Verdict.REFUTED else ("emulator_error" if rejected else "ok")
            else:
                outcome = "caught" if result.verdict is Verdict.REFUTED else ("emulator_error" if rejected else "missed")
            outcomes.append({"id": case["id"], "label": case["label"], "outcome": outcome, "reason": result.reason})
            print(f"{outcome:15} {case['label']:15} {case['id']}  {result.reason[:100]}")
    wrong = [o for o in outcomes if o["outcome"] == "WRONG"]
    print(f"\n{len(outcomes)} cases, {sum(o['outcome'] == 'caught' for o in outcomes)} caught, "
          f"{sum(o['outcome'] == 'emulator_error' for o in outcomes)} emulator errors, {len(wrong)} wrong")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(outcomes, indent=1))
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
