# BigQuery shapes (synthetic)

**Purpose:** check that the emulator oracle can tell apart query pairs written in real BigQuery and Dataform shapes: STRUCT and ARRAY, QUALIFY, UNNEST, incremental-model filters, CTE stacks, NULL handling, alias scope, literal coercion.

**Example:** `SELECT id, tag FROM orders CROSS JOIN UNNEST(tags) AS tag` against the same query with `LEFT JOIN UNNEST`. They differ on a row with an empty array, so the corpus builds one and the oracle should refute the pair.

**Corpus:** [`benchmarks/bq_shapes/cases.json`](../../benchmarks/bq_shapes/cases.json), 14 pairs (8 equivalent, 6 not), all synthetic. Labels are the authors' reading of BigQuery semantics. Two of them (alias scope in ORDER BY, NUMERIC literal coercion) repeat facts checked on real BigQuery in an earlier KumoSQL thread; the rest are textbook semantics.

**Score:** not measured. The first live run of `python tools/run_oracle_eval.py` is pending (see [emulator-decision.md](../emulator-decision.md)). Results go to `benchmarks/results/bq-shapes-oracle.json`.

**Limits:** it measures refutation only. It cannot show that a "not equivalent" label is right, only that the emulator and the label agree. Held-out split: none yet; this corpus is a development set.
