# Kumo BigQuery Prover

An equivalence prover that assumes BigQuery exclusively. Query pairs are parsed as BigQuery SQL and, when they must be run, executed on the BigQuery emulator. There is no dialect translation and no DuckDB.

Status: proofs and refutations both work. Four static layers (identical, structural, algebraic, SMT) are ported from Kumo_SQL_Prover with every other-engine path removed ([docs/ported-layers.md](docs/ported-layers.md)); the emulator refutes a pair with a counterexample and checks every proof on built databases. Documented rewrites: 18 of 19 equivalent pairs proved; numeric traps: 51 of 65 proved; a held-out set frozen before the layers ran on it: 28 of 39 proved, 23 of 24 different pairs refuted; a proof audit over 1,186 pairs from the upstream tests: 411 proved, 0 unexplained refutations. 0 false proofs in all ([docs/evals/pairs.md](docs/evals/pairs.md), [docs/evals/heldout.md](docs/evals/heldout.md), [docs/evals/proof-audit.md](docs/evals/proof-audit.md)). See [docs/roadmap.md](docs/roadmap.md).

```python
from kumo_bq_prover.emulator import running_emulator
from kumo_bq_prover.prove import prove_equivalent

with running_emulator() as client:
    result = prove_equivalent(
        "SELECT x FROM UNNEST([1, 2, 3]) AS x",
        "SELECT x FROM UNNEST([1, 2]) AS x",
        client=client,
    )
print(result.verdict)  # Verdict.REFUTED, with both row sets in result
```

Pass `schema={"table": {"column": "INT64"}}` so the static layers know the column types. A proof lists its `assumptions`; two of them (no NaN, SUM independent of row order) are modelling choices, so read them.

Verdicts: `PROVEN` (sound), `REFUTED` (a concrete input with different rows), `UNKNOWN` (everything else, including "agreed on the data it saw").

## Setup

```sh
pip install -e ".[dev]"                          # includes z3, which the proof layers need
tools/install_emulator.sh                       # builds the pinned emulator with Go
export KUMO_BQ_EMULATOR_BIN=$PWD/.emulator/bin/bigquery-emulator
python -m pytest                                # offline tests, including the ported proof-layer tests (about 2 minutes)
python -m pytest -m emulator                    # live tests, need the binary
python tools/run_oracle_eval.py                 # score the synthetic corpus
```

Which evals to improve this against, in order: [docs/hillclimb-evals.md](docs/hillclimb-evals.md). Pair corpora: `python tools/run_pairs_eval.py documented-rewrites`, `numeric-traps` or `heldout-2026-11-12` (never tune on the last). Proof audit: `python tools/audit_proofs.py benchmarks/audit/upstream-test-pairs.jsonl`.

Docs: [docs/README.md](docs/README.md).
