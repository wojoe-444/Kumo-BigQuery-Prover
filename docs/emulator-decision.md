# Decision: which BigQuery emulator is the oracle

Decided 2026-11-11. Chosen: **goccy/bigquery-emulator**, pinned at v0.8.1 in `tools/install_emulator.sh`.

## Why this page exists

This prover assumes BigQuery only. When a query pair needs running (to find a counterexample, or to check a label), the run must happen on something that behaves like BigQuery. Translating BigQuery SQL into another engine's dialect and running it there was the problem the first prover kept hitting, so the oracle must read BigQuery SQL natively.

## The candidates

| | goccy/bigquery-emulator | jjviscomi/bqemulator |
|---|---|---|
| Front end | GoogleSQL's own parser and analyzer (`go-googlesql`, compiled from Google's GoogleSQL sources) | sqlglot rewrites GoogleSQL into another dialect |
| Executes on | SQLite, with BigQuery functions and types implemented in Go | DuckDB |
| Name resolution, coercion, type checking | Google's analyzer, the same code family BigQuery uses | Re-implemented by translation rules |
| Install | `go install`, one static binary; Docker image exists | `pip install`, Docker image |
| Licence | MIT | Apache 2.0 |
| Maturity | beta, one maintainer, v0.8.1 | self-described stable, young |

**Rejected: jjviscomi/bqemulator.** It is DuckDB underneath, behind a translation layer. That is the rabbit hole we are leaving, only hidden inside a dependency. Its claimed 1,288 conformance fixtures are the maintainer's own numbers; we did not check them.

**Chosen: goccy/bigquery-emulator.** The part of BigQuery that caused our earlier false proofs is name resolution and literal coercion (alias scope in ORDER BY and QUALIFY, `NUMERIC` literals). That part is Google's analyzer here, not a re-implementation. Execution semantics are re-implemented in Go and will have gaps; the plan is to find them by running, not by reading.

Not evaluated: Google's reference implementation inside the GoogleSQL repository (needs a Bazel build and is a test driver, not a service), and real BigQuery dry runs (no credentials in cloud threads, and they never return rows).

## What was checked in this thread

- The binary builds from source with `go install` in a cloud container (about three minutes, no cgo).
- The source of v0.8.1 supports `QUALIFY`, `STRUCT`, `ARRAY`, `UNNEST`, wildcard tables and JavaScript UDFs, and the flags this repository passes (`--project`, `--host`, `--port`, `--grpc-port`) exist.
- **Not checked: any run of the emulator.** The sandbox the thread ran in refused to execute the downloaded binary, so every live statement is unverified. `tests/test_emulator_live.py` and `tools/run_oracle_eval.py` are written and wait for a first run.

## Rules for using the emulator

1. The oracle only refutes. Two queries that agree on built data are `UNKNOWN`, never `PROVEN`.
2. A statement the emulator rejects makes a comparison `UNKNOWN`. Its limits are not BigQuery's behaviour.
3. An equivalent-labelled pair the emulator refutes is a bug to look at first as an emulator bug, second as a bad label. It is never silently dropped.
4. A BigQuery semantic is confirmed on real BigQuery (dry run or inline values, 0 bytes billed) before the prover relies on it, not on the emulator alone.
5. Known gap to handle: one query erroring while the other returns rows (division by zero against `SAFE_DIVIDE`) is a real difference, but the oracle currently reports it as `UNKNOWN`.

## When to switch

Switch only on evidence from `tools/run_oracle_eval.py` and later corpora: wrong results the emulator gives for BigQuery constructs we need, counted per construct. Until then we accept its limits.
