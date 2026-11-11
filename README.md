# Kumo BigQuery Prover

An equivalence prover that assumes BigQuery exclusively. Query pairs are parsed as BigQuery SQL and, when they must be run, executed on the BigQuery emulator. There is no dialect translation and no DuckDB.

Status: skeleton. The static proof layer only recognises identical queries; the emulator can refute a pair with a counterexample. First live run: 5 of 6 non-equivalent synthetic pairs refuted, 0 wrong (see [docs/evals/bq-shapes.md](docs/evals/bq-shapes.md)). See [docs/roadmap.md](docs/roadmap.md).

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

Verdicts: `PROVEN` (sound), `REFUTED` (a concrete input with different rows), `UNKNOWN` (everything else, including "agreed on the data it saw").

## Setup

```sh
pip install -e ".[dev]"
tools/install_emulator.sh                       # builds the pinned emulator with Go
export KUMO_BQ_EMULATOR_BIN=$PWD/.emulator/bin/bigquery-emulator
python -m pytest                                # offline tests
python -m pytest -m emulator                    # live tests, need the binary
python tools/run_oracle_eval.py                 # score the synthetic corpus
```

Which evals to improve this against, in order: [docs/hillclimb-evals.md](docs/hillclimb-evals.md). Pair corpora: `python tools/run_pairs_eval.py documented-rewrites` or `numeric-traps`.

Docs: [docs/README.md](docs/README.md).
