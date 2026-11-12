# Proof audit

**Purpose:** an independent check of every proof the static layers give, at a scale no hand-written corpus reaches. Kumo_SQL_Prover's own test suite hands its provers thousands of query pairs. The audit takes those pairs, lets the ported layers prove what they can, and runs every proved pair on the emulator's seeded databases. A proved pair the emulator refutes is a false proof until shown otherwise.

**Example:** `SELECT a AS k FROM t` and the `UNION ALL` of the two halves of a filter that partitions it are proved equal. The audit builds random `t` tables, runs both on the emulator, and finds the same rows. (It also refuted this pair once, on a NULL; real BigQuery agreed with the proof and the emulator was wrong, see below.)

**Corpus** (`benchmarks/audit/upstream-test-pairs.jsonl`, 1,186 pairs): every pair recorded when the upstream test suite (commit `ff364a7`) called `equivalence.prove_equivalent`, `algebraic_equivalence.prove_equivalent_algebraic` or `smt_equivalence.prove_equivalent_smt` in the BigQuery dialect, with its schema and types. Recorded by `tools/record_prover_calls.py`, a pytest plugin. The recording ran the whole upstream suite with an engine stand-in that raises on use, so the tests that were not ported (they replay a witness database on another engine) still contributed the pairs they handed to the prover before failing at their engine call. Pairs that passed constraints (keys, NOT NULL, foreign keys) are left out: the audit's data generator does not build data that obeys them yet.

**Run:** `python tools/audit_proofs.py benchmarks/audit/upstream-test-pairs.jsonl --databases 8 --workers 4`. Data for a table the pair declared uses exactly its declared columns and order; data for what it did not declare comes from a guess (`src/kumo_bq_prover/infer.py`).

## Result (2026-11-12, emulator v0.8.1)

| Outcome | Pairs |
|---|---|
| Not proved by any layer | 774 |
| Proved, checked on the emulator, no refutation | 325 |
| Proved, not runnable on the emulator (rejected statement or no schema) | 63 |
| Proved, with a volatile call left identical on both sides | 21 |
| Proved, refuted, and real BigQuery agrees with the proof (emulator bug) | 2 |
| Proved, refuted, and the proof lists the SUM-order assumption | 1 |
| Proved, refuted, unexplained | 0 |

Proofs by layer: identical 56, structural 47, algebraic 287, SMT 19. Results file: `benchmarks/results/proof-audit.json`.

## What it found

- **A false proof:** identical text was proved equal even for `SELECT APPROX_TOP_COUNT(id, 2) AS x FROM t`, whose two runs differ when counts tie. The identical layer now needs the structural layer to agree, which refuses nondeterministic queries.
- **Two emulator bugs** (wrong results, both confirmed on real BigQuery with inline values, 0 bytes billed; recorded in `benchmarks/audit/known-emulator-bugs.json`): `x IS NOT TRUE` returns NULL instead of TRUE when `x` is NULL, and a `UNION ALL` branch picks up the columns of an earlier branch that repeated an expression (`SELECT a, a AS b FROM u UNION ALL SELECT a, b FROM u` returns `(1, 1)` twice where BigQuery returns `(1, 1)` and `(1, 2)`).
- **The SUM-order case:** a pair with `SUM` over FLOAT64 values past 2**53, proved by the SMT layer under the disclosed assumption that `SUM` ignores row order. The emulator's two sums differ in the last digits. This is the FLOAT64 SUM labelling question, left to Walter.
- **Three artifacts of the audit's own data**, fixed in the audit: a column guessed for a declared table changes how a bare name resolves, and the declared column order matters for `SELECT *`.

## Limits

- The earlier prover's rules were written against many of these pairs, so a high proved count is recall on tuned data.
- Eight random databases cannot find a difference that needs a rare input (a value past 2**53 beside small ones, a NULL in a join key). The FLOAT64 regrouping false proof in [pairs.md](pairs.md) was not found this way.
- The emulator cannot hold NaN or -0.0, so the no-NaN assumption is never tested.
- 63 proved pairs could not be run at all, so they have no emulator evidence.
- The audit does not run the proofs' assumptions through real BigQuery; only the three refutations were checked there.
