# BigQuery shapes (synthetic)

**Purpose:** check that the emulator oracle can tell apart query pairs written in real BigQuery and Dataform shapes: STRUCT and ARRAY, QUALIFY, UNNEST, incremental-model filters, CTE stacks, NULL handling, alias scope, literal coercion.

**Example:** `SELECT id, tag FROM orders CROSS JOIN UNNEST(tags) AS tag` against the same query with `LEFT JOIN UNNEST`. They differ on a row with an empty array, so the corpus builds one and the oracle should refute the pair.

**Corpus:** [`benchmarks/bq_shapes/cases.json`](../../benchmarks/bq_shapes/cases.json), 14 pairs (8 equivalent, 6 not), all synthetic. Labels are the authors' reading of BigQuery semantics. Two of them (alias scope in ORDER BY, NUMERIC literal coercion) repeat facts checked on real BigQuery in an earlier KumoSQL thread; the rest are textbook semantics.

**Score (2026-11-11, emulator v0.8.1):** the oracle refutes 5 of the 6 not-equivalent pairs and refutes none of the 8 equivalent ones (0 wrong, 0 emulator errors). Recorded in `benchmarks/results/bq-shapes-oracle.json`.

**What the run showed about the emulator:**
- Missed pair: `LEFT JOIN UNNEST(tags)` with no `ON` drops the row whose array is empty. Real BigQuery keeps it (checked with inline values, 0 bytes billed); the emulator keeps it only with `ON TRUE`. This is a wrong result, not an error, so it is the kind of gap to watch.
- Unary minus (`-id`) fails with "no such function: googlesqlite_unary_minus". The alias-scope case is written as `0 - id` for now.
- Alias scope in ORDER BY and NUMERIC literal coercion both behave as BigQuery does here.

**Limits:** it measures refutation only. It cannot show that a "not equivalent" label is right, only that the emulator and the label agree. Held-out split: none yet; this corpus is a development set.
