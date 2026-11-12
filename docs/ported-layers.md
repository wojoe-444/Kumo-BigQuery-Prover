# The ported proof layers

**Purpose:** prove that two BigQuery queries return the same rows on every input, without running anything. The layers come from [Kumo_SQL_Prover](https://github.com/wojoe-444/Kumo_SQL_Prover) (commit `ff364a7`) with every path that executes queries on another engine removed. They live in `src/kumo_bq_prover/ported/`; `src/kumo_bq_prover/static.py` runs them in order and decides what counts as `PROVEN`.

**Example:** `SELECT CASE WHEN qty IS NULL THEN 0 ELSE qty END FROM items` and `SELECT COALESCE(qty, 0) FROM items` are proved equal by the algebraic layer. `SELECT x FROM t WHERE x > 1` and `... WHERE x >= 1` are not proved, and the emulator can refute them with a database holding `x = 1`.

```python
from kumo_bq_prover.prove import prove_equivalent
result = prove_equivalent(left_sql, right_sql, schema={"items": {"qty": "INT64"}})
result.verdict      # PROVEN, REFUTED (needs client=...) or UNKNOWN
result.reason       # "algebraic: bag semantics: matching blocks are equivalent"
result.assumptions  # read these: see "What a proof assumes"
```

## The four layers

Cheapest first; the first proof ends the search.

| Layer | Module | What it does |
|---|---|---|
| `identical` | `static.py` | both queries parse to the same tree |
| `structural` | `ported/equivalence.py` | CTE lifting and ordering, predicate and literal canonical forms, bag comparison |
| `algebraic` | `ported/algebraic_equivalence.py` | rule-driven normaliser (joins, aggregates, set operations, windows, NULL facts), then the SMT prover on the normal forms |
| `smt` | `ported/smt_equivalence.py` | the SMT prover (z3) on the queries as written |

169 modules, about 53,000 lines, keep the names they have upstream so a fix can be carried across.

## What was cut, and what was changed

Cut:
- Every path that runs queries on another engine: the counterexample search of the algebraic layer, the data-independence check of the conditional layer, the engine-specific printing helper and the engine's own parser family. The conditional layer (`PROVEN_CONDITIONALLY`) is present but fails closed: its independence check reports an error, so no conditional proof is produced.
- Modules that exist only to run queries: bounded execution, counterexample generation, result comparison on built data, the targeted-data builders. The emulator replaces them through `oracle.py`.

Not cut: the dialect tables for MySQL and PostgreSQL inside modules such as `parse_check.py`. They are dormant, because every entry point passes `dialect="bigquery"`. Removing them is a later cleanup, not a soundness question.

Changed from upstream, and not yet reported there:
- **A false proof, fixed.** `SELECT x FROM (SELECT 1 AS x, SUM(COUNT(*)) OVER () AS w FROM events) AS d` and `SELECT 1 AS x FROM events` are proved equal upstream. The left returns one row (the `COUNT(*)` makes the select a global aggregate); the right returns one row per event. Window pruning treated any aggregate inside a window as "not an aggregate of the select". `ast_utils.is_window_head` now separates the function a window applies from an aggregate among its arguments, and three modules use it (`window_pruning`, `window_filters`, `window_sources`). Other modules use the same `find_ancestor(exp.Window)` test and were not changed; they decline or accept in the safe direction on the shapes checked, but that is a review item, not a proof.
- Stripped the engine name from dialect tables and comments, so `tests/test_no_duckdb.py` can scan all of `src/`.

## How `static.py` decides `PROVEN`

1. **Bag semantics only.** With `ordered=True` only an identical parse is proved. An identical parse is proved only when the structural layer agrees, because identical text is not enough for a query that can answer differently on two runs (`APPROX_*`, a window with ties); found by the proof audit.
2. **Runtime errors.** The SMT layer reports what a rewrite does to errors (`none`, `same`, `refines`, `introduces`, `unknown`). A proof is kept only for `none` or `same`. `refines`, `introduces` and `unknown` withhold it, and the reason goes in `StaticResult.withheld`. The algebraic layer always reports `unknown`, because normalisation moves the operations; for it, a pair whose syntax cannot fail (`static.cannot_fail`: no arithmetic, cast, scalar subquery or SUM) is treated as `none`.
3. **FLOAT64 sums.** The algebraic layer reads `SUM` as real arithmetic, so it regroups partial sums, and FLOAT64 addition is not associative (`1e16 + 1 + 1 + 1` is not `(1e16 + 1) + (1 + 1)`). The layer is skipped for any pair with a `SUM` whose input may be FLOAT64: a FLOAT64 column, a column of unknown type, a decimal literal, a division, and the order-sensitive aggregates (`AVG`, `STDDEV`, `VARIANCE`, `CORR`, `COVAR`). An alias is followed to the expression it names. The SMT layer still runs on such pairs.
4. **A layer that raises proves nothing.**

## What a proof assumes

`result.assumptions` lists them. Two are modelling choices, not facts about the data:
- `FLOAT64 values are never NaN`. BigQuery data can hold NaN, and the emulator cannot hold it (it reads back as NULL), so the oracle cannot cross-check this.
- `SUM and AVG are treated as independent of row order`. BigQuery does not promise a FLOAT64 SUM order. This is the open FLOAT64 SUM labelling question (left to Walter).

The others are disclosures: result column types are not compared (confirm schemas with a BigQuery dry run), and runtime errors are covered as described above.

## How it is checked

- **Ported tests** (`tests/ported/`): 139 files, 3,098 test cases, all passing with no extra engine installed. They are the upstream tests with the ones that replay a witness database on another engine removed, because that is what they were for; 97 upstream files had no test left or read files from the upstream repository. The removed witness tests are replaced by the proof audit below, which reaches the same pairs through the prover call and checks them on the emulator.
- **Proof audit:** [evals/proof-audit.md](evals/proof-audit.md): every pair the upstream tests give their provers (1,186), proved by the port and run on the emulator. 411 proved, 0 unexplained refutations; it found one false proof (identical text, nondeterministic query) and two emulator bugs.
- **Held-out set:** [evals/heldout.md](evals/heldout.md): 63 pairs frozen before the layers ran on them. 28 of 39 equivalent proved, 20 of 24 different refuted (23 of 24 without the trust guard), 0 false proofs.
- **False-proof detector:** `tools/run_pairs_eval.py` runs every pair the layers prove on the seeded databases of the emulator. A proved pair the emulator refutes is `PROOF_REFUTED`, a proved pair labelled different is `FALSE_PROOF`, and the script exits 1 on either. See [evals/pairs.md](evals/pairs.md) for the scores.
- **What the detector cannot see:** a pair whose difference needs data the generator rarely draws. The FLOAT64 regrouping false proof was not found by 8 random databases; the label found it.

## Known: run the suite in parallel

`pytest` is configured with `-n auto` (pytest-xdist, in the `dev` extra). Run serially (`-p no:xdist`), the last nonlinear NUMERIC refutation in `tests/ported/test_smt_numeric.py` (`..._reference_arithmetic_agrees_with[3]`) can hit its 8 second solver limit and fail: it finishes within a second of the limit, and in one long process the shared Z3 context has grown with every earlier test. Under xdist it passes (checked on three runs). Resetting the Z3 context between tests breaks the module-level terms the tests share, so it is not done. The cause is a solver time limit near its edge, not a wrong answer; if it fails on your machine, raise `timeout_ms` for that test or run it alone.

## Limits

- The ported layers were tuned on corpora that this repository uses as development sets, so scores on them say little about new pairs ("tuned on test").
- No held-out split exists yet in either corpus.
- The layers are not proved correct. They are tested and cross-checked; a false proof is a bug.
- Proof time is not bounded here except by the solver limits inside the layers; a pair can take seconds.
