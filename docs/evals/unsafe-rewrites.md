# Unsafe-rewrite pairs

**Purpose:** score the emulator oracle on 560 pairs of BigQuery queries over two INT64 tables where a rewrite looks valid and often is not: `NOT IN` with NULLs, `COUNT(*)` against `COUNT(col)`, joins that repeat rows, `INTERSECT` against a join, predicates moved across an outer join. Each unsafe rewrite sits next to a correct variant. The pairs come from KumoSQL's unsafe-rewrite fuzzing fixture (BigQuery dialect, written for BigQuery from the start); this repository only scores them.

**Example:** `SELECT t.a FROM t WHERE t.a NOT IN (SELECT u.b FROM u)` against `... WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)`. When `u.b` holds a NULL the first returns nothing and the second returns every row of `t` without a match. On real BigQuery with `t.a` of 1 and NULL and `u.b` of NULL and 2: 0 rows against 1 row (checked 2026-11-12, 0 bytes billed).

**Files:** `benchmarks/pairs/unsafe-rewrites.jsonl` (`family`, `heldout` as KumoSQL marked it, `emulator_refuted`), result `benchmarks/results/pairs-unsafe-rewrites.json`.

**Run:** `python tools/run_pairs_eval.py unsafe-rewrites --databases 16 --layers none`. Each pair runs on 16 seeded databases of two INT64 tables `t` and `u` (columns `a`, `b`, `c`), values from 0 to 3 with duplicates, up to six rows each. Even seeds add NULL to the values (about one cell in five, which `INTERSECT` and `NOT IN` need); odd seeds do not (a NULL key never joins, which hides the join pairs).

## Scores (emulator v0.8.1, refutation only)

| | Count |
|---|---|
| Equivalent pairs (220) left alone | 220, none refuted |
| Different pairs (340) refuted | 240 |
| Different pairs refuted but demoted to unknown by the trust guard | 100 |
| Different pairs missed | 0 |

24 of the 340 were not refuted in the first 16 databases and were found within 64 (`databases_needed` in their record). 0 wrong: no equivalent pair is refuted. Nothing is proved; the run uses `--layers none`.

## What the labels are

The 220 correct variants are labelled equivalent by hand, and the run checks them: none is refuted by 16 databases, which is evidence, not a proof. The other 340 were the earlier prover's `either` ("may differ"): it refuted all of them with a DuckDB search. Here a pair is labelled not_equivalent because this oracle found a database on which the rows differ, so those 340 labels are **not independent evidence**; that half of the score measures recall of the data generator, not label quality.

**Demoted (100).** The trust guard ([../emulator-gaps.md](../emulator-gaps.md)) answers UNKNOWN for any difference involving `IN`, because the emulator gets `3 IN (1, 2, NULL)` wrong. All 100 are `NOT IN` or `IN` pairs (five shapes: `not_in-exists`, `not_in-antijoin`, `not_in-notnull-filter`, `join_dupes-in`, `join_dupes-distinct-semijoin-agg`). One emulator counterexample per shape was replayed on real BigQuery with the same rows inlined, and BigQuery returned the emulator's rows: `[0,3,3]` against `[null,0,3,3]` (NOT IN against NOT EXISTS and against the anti-join), `[]` against `[100]` (NOT IN with and without the NULL filter), `[0,0]` against `[0]` (join against IN), `2` against `1` (SUM over the join against SUM over IN). The other pairs of each shape were not replayed one by one.

## Limits

- Tuned on test: after the first run I changed how the generator draws NULLs (the even and odd seeds above) because a mix of NULL-free and NULL-heavy databases refuted more pairs than either alone; the number of databases was raised to 64 for the 24 late pairs. The corpus has no held-out split of its own (KumoSQL's `heldout` flag is kept in each record: 140 pairs, all scored the same way).
- The data is small and uses the domain 0 to 3 the pairs were written against, so it finds these shapes quickly but says little about other values.
- Results are compared as multisets, so a pair that differs only in row order counts as agreeing.
