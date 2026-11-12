# Kumo BigQuery Prover

An equivalence prover that assumes BigQuery exclusively. Query pairs are parsed as BigQuery SQL and, when they must be run, executed on the BigQuery emulator. There is no dialect translation and no DuckDB.

Status: proofs and refutations both work. Four static layers (identical, structural, algebraic, SMT) are ported from Kumo_SQL_Prover with every other-engine path removed ([docs/ported-layers.md](docs/ported-layers.md)); the emulator refutes a pair with a counterexample and checks every proof on built databases. Documented rewrites: 18 of 19 equivalent pairs proved; numeric traps: 51 of 65 proved, 0 false proofs ([docs/evals/pairs.md](docs/evals/pairs.md)). See [docs/roadmap.md](docs/roadmap.md).

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

Verdicts: `PROVEN` (sound), `REFUTED` (a concrete input with different rows), `UNKNOWN` (everything else, including "agreed on the data it saw" and "different on the emulator, on a construct or value it gets wrong").

The emulator is not BigQuery. Google's GoogleSQL compliance cases measured where it disagrees with the reference ([docs/emulator-gaps.md](docs/emulator-gaps.md)); a difference that involves such a construct or an extreme value is reported UNKNOWN with the reasons in `result.untrusted`, never REFUTED. Proofs are not affected: they never rest on the emulator.

## Setup

```sh
pip install -e ".[dev]"                          # includes z3, which the proof layers need
tools/install_emulator.sh                       # builds the pinned emulator with Go
export KUMO_BQ_EMULATOR_BIN=$PWD/.emulator/bin/bigquery-emulator
python -m pytest                                # offline tests, including the ported proof-layer tests (about 2 minutes)
python -m pytest -m emulator                    # live tests, need the binary
python tools/run_oracle_eval.py                 # score the synthetic corpus
```

Which evals to improve this against, in order: [docs/hillclimb-evals.md](docs/hillclimb-evals.md). Pair corpora: `python tools/run_pairs_eval.py documented-rewrites` or `numeric-traps`. Pairs from the Spider 2.0 gold queries: `python tools/spider2_pairs.py --split dev` ([docs/evals/spider2.md](docs/evals/spider2.md)). Compliance cases on the emulator: `python tools/run_compliance.py --split all --json RESULTS.json`, then `python tools/compliance_gaps.py RESULTS.json` ([docs/evals/googlesql-compliance.md](docs/evals/googlesql-compliance.md)).

Docs: [docs/README.md](docs/README.md).
