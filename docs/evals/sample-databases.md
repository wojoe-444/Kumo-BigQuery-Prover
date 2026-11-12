# Sample-database pairs (constraint-aware)

**Purpose:** score the emulator oracle on 286 query pairs over six public sample databases (Chinook, Northwind, Sakila, Pagila, Oracle HR, Oracle Customer Orders), where most pairs are equivalent only because of declared keys. The pairs are in BigQuery dialect over BigQuery DDL, so no translation is involved. They come from KumoSQL's sample-database eval, authored there, and are not upstream cases.

**Example:** `SELECT t.TrackId FROM Track t JOIN MediaType m ON t.MediaTypeId = m.MediaTypeId` against `SELECT TrackId FROM Track` is equivalent because `MediaTypeId` is `MediaType`'s key and `Track.MediaTypeId` is a NOT NULL foreign key to it. Its sibling `...-without-fk` drops the foreign key (`"drop": ["fk:Track.MediaTypeId"]`) and is labelled different: a track may name a media type that does not exist.

**Files:** `benchmarks/pairs/sample-databases.jsonl` (the pairs; `database` names the schema, `drop` the declarations a sibling lacks, `witness` the authored database or `"real"`), `benchmarks/pairs/schemas/<database>.sql` (the adapted BigQuery DDL with its upstream attribution header; no data). Result: `benchmarks/results/pairs-sample-databases.json`.

**Run:** `python tools/run_pairs_eval.py sample-databases --databases 16 --layers none`.

## How the data is built

An equivalent pair must never be refuted on a database that breaks the declarations it depends on, so `src/kumo_bq_prover/constrained.py` reads the DDL (primary keys, NOT NULL, foreign keys, all `NOT ENFORCED` in BigQuery) and builds each database under it:

- only the tables the two queries read are created; keys are unique and never NULL; a foreign key takes a real parent key (or NULL when the column is nullable); a self-reference or a cycle of foreign keys (Sakila's `store` and `staff`) is repaired until every key holds;
- the pair's `drop` list is ignored for that pair (`pk:T`, `fk:T.c`, `not_null:T.c`), which is how the siblings find their counterexamples;
- the constants in the two queries are offered to the columns they are compared with (the literal harvester), and the pair's `witness` rows, when it has a small authored database, run last with the other columns completed by legal values and a parent row for every foreign key still declared.

A `"real"` witness means the two queries already differ on the upstream sample data. This repository does not load that data, so those pairs depend on random data to be refuted.

## Scores (emulator v0.8.1, 16 databases per pair, refutation only)

| | Count |
|---|---|
| Equivalent pairs left alone | 118 of 121 |
| Equivalent pairs refuted | 0 |
| Equivalent pairs the emulator cannot run | 3 |
| Different pairs refuted | 156 (132 plus the 24 the trust guard used to demote) |
| Different pairs refuted but demoted to unknown by the trust guard | 0 |
| Different pairs missed | 3 |
| Different pairs not refuted, with emulator errors on some databases | 2 |
| Different pairs the emulator cannot run | 4 |

Nothing here is proved: the run uses `--layers none`, because proofs need the declared keys, which the ported layers do not read yet. 0 wrong.

**Formerly demoted (24):** the trust guard ([../emulator-gaps.md](../emulator-gaps.md)) used to answer UNKNOWN for any difference involving `IN` or `NOT IN`, because the emulator gets `3 IN (1, 2, NULL)` wrong. All 24 had one query with an `IN` or `NOT IN`, in pairs labelled different because of NULLs or duplicate keys (the `INTERSECT` and `EXCEPT` pairs among them are rewritten to `IN`). The guard now re-runs such a difference with the `IN` written as `OR` of `=` or as a `CASE` over `EXISTS`; all 24 still differ, so they count as refuted with the rewritten queries' rows. The NOT IN, IN, INTERSECT and join semantics those pairs rely on were checked on real BigQuery with inline values (see [unsafe-rewrites.md](unsafe-rewrites.md)); the individual 24 counterexamples were not replayed on BigQuery.

**Missed (3):** `co-window-filter-on-other-column`, `pg-window-filter-on-other-column` and `pg-in-to-join-fan-out`, all `"real"` witnesses that need rows random data did not draw.

**Emulator gap (9 pairs):** a table named `jobs` can be created on the emulator but not read or written afterwards ("Column job_id is not present in table jobs"), so every Oracle HR pair that reads `jobs`, directly or through the completed witness, is an emulator error or an incomplete run. Real BigQuery has no such restriction.

## What the run found about the labels

- **A label that is wrong in BigQuery, fixed.** `sk-fk-chain-join-elimination` compared `... FROM address AS a JOIN city ...` with `SELECT address_id, address FROM address`. In BigQuery an unqualified name that is both a column and the table's own name resolves to the table, so the right query returns `address` as a STRUCT of the whole row. Checked on real BigQuery with a CTE named `address` (0 bytes billed, 2026-11-12); the emulator agrees. The pair is now labelled different with a note, and `sk-fk-chain-join-elimination-aliased` (the right side aliased) carries the intended equivalence, which the emulator leaves alone.
- No other equivalent pair was refuted, so the other 120 equivalent labels survived 16 constraint-respecting databases each. That is evidence, not a proof.

## Limits

The data is small (up to four rows a table), so a pair that needs a specific value or a long chain of matching keys can be missed. The generator makes the databases legal under the declarations only; two constraints the upstream databases carry are not declared in BigQuery DDL (UNIQUE keys) and are dropped, as in KumoSQL, so Oracle CO pairs that would need them are labelled different. The labels were set by the earlier DuckDB-based prover and its authored witnesses, not by BigQuery, which is why the emulator run is a check on them and not their source.
