# Labelled pair corpora on the emulator

**Purpose:** score how often the emulator oracle tells equivalent pairs from different ones on pairs written in BigQuery, and find where the emulator or the labels are wrong. These are the first two hillclimb evals (see [hillclimb-evals.md](../hillclimb-evals.md)).

**Example:** `SELECT CAST(f AS INT64) FROM t` against `SELECT CAST(ROUND(f) AS INT64) FROM t` is labelled equivalent. On a database where `f` is 2.5 the emulator returns 2 for the left query and 3 for the right, so the oracle refutes an equivalent pair. Real BigQuery returns 3 for both, so the emulator is wrong, not the label.

**Corpora** (`benchmarks/pairs/`, copied from KumoSQL fixtures, which its threads authored): `documented-rewrites.jsonl` (25 pairs, rewrites that documentation recommends) and `numeric-traps.jsonl` (112 pairs, BigQuery numeric and coercion traps). Both are development sets with no held-out split; they were written before this prover existed, but their rules were tuned on them in the earlier prover.

**Run:** `python tools/run_pairs_eval.py <corpus> --databases 8`. Each pair runs on 8 seeded random databases (and the pair's own witness rows when it has them). The emulator is restarted every 6 pairs because it slows down as tables accumulate.

## Scores (2026-11-11, emulator v0.8.1, 8 databases)

| Corpus | Equivalent: not refuted | Equivalent: refuted | Equivalent: emulator error | Different: refuted | Different: missed | Different: emulator error |
|---|---|---|---|---|---|---|
| Documented rewrites (19 + 6) | 19 | 0 | 0 | 1 | 4 | 1 |
| Numeric traps (65 + 26, 21 error-class unscored) | 61 | 1 | 3 | 11 | 14 | 1 |

Nothing is proved yet: the static layer only recognises identical queries. These are refutation scores, results files `benchmarks/results/pairs-*.json`.

## What the runs found

Checked on real BigQuery (inline values, 0 bytes billed) unless noted.

Emulator problems:
- **Wrong result:** `CAST(2.5 AS INT64)` is 2 on the emulator, 3 on BigQuery. This is the dangerous kind: it would refute a true equivalence.
- `1e309` as a literal makes the emulator fail while formatting the query; BigQuery accepts it.
- Unary minus (`-x`) is missing.
- `LEFT JOIN UNNEST(arr)` without `ON` drops rows with an empty array (found in the first run, see [bq-shapes.md](bq-shapes.md)).

Corpus problems (the emulator agrees with BigQuery, the labels do not):
- `R012b-09` uses `qty = NULL`, which BigQuery refuses to compile. The case is not valid BigQuery.
- `numeric-literal-rounding` and `numeric-literal-below-half` use NUMERIC literals with ten decimals, which BigQuery rejects. Rounding happens in casts, not literals.

Each of these cases carries a `bigquery_note` in its record.

**Misses (headroom):** all 18 missed pairs need a specific value the random pools lack: a mid-year timestamp, two rows with one key, a code starting with `HN-`, a value on a boundary. The first lever is a generator that harvests the literals in the two queries and adds them, with neighbours, to the value pools.

**Limits:** results are compared as multisets, so a pair that differs only in row order counts as agreeing. Both queries raising an error on the same database counts as an error, not an agreement. A refutation is only as good as the emulator's answer on that construct, which is why every WRONG is looked at by hand before anything else.
