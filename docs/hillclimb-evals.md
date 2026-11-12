# Evals to hillclimb

Written 2026-11-11. Purpose: pick the BigQuery-focused evals this prover should be improved against, in order, and say what each one can and cannot show.

An eval is worth hillclimbing here when it has four things:

1. **BigQuery is the source dialect**, not a translation. Corpora written for MySQL, PostgreSQL or Calcite (Singh, VeriEQL, QED, SQL-RewriteBench, which is PostgreSQL over TPC-DS and DSB) are out.
2. **Labelled pairs** (equivalent or not), so a score is proved, refuted or unknown against a known answer.
3. **Data the emulator can run**: a small synthetic schema, or a schema the generator can fill. No credentials, no hosted tables.
4. **Headroom**: unproved or unrefuted cases left to win.

Everything below that already exists is a KumoSQL fixture (authored by KumoSQL threads, so the same licence as that repository) or a pinned open-source corpus. Numbers marked "KumoSQL record" are what the earlier DuckDB-based prover scored; they are the headroom estimate, not a measurement of this prover.

## Ranked shortlist

| # | Eval | Size | KumoSQL record | Why it earns a place | Cost to start |
|---|---|---|---|---|---|
| 1 | Numeric traps | 112 (65 equivalent, 26 not, 21 error-class) | 50/65 equivalent proved, 11/26 refuted | BigQuery numeric semantics (INT64 past 2**53, NUMERIC rounding, implicit coercion) are exactly where DuckDB gave wrong answers. 16 cases carry concrete witness rows. Largest headroom in the list | Ported here, baseline below |
| 2 | Documented rewrites | 25 (19 equivalent, 6 not) | 18/19 proved, 5/6 refuted | Rewrites that vendor and style-guide documentation recommends, in BigQuery dialect. Small, readable, good for the first data-generation work | Ported here, baseline below |
| 3 | Pipeline equivalence and refutation | 50 + 66 generated Dataform pipelines | 50/50 proved and 66/66 refuted, but held-out families 3/8 proved | The practical target: does a refactor spread over several models keep every observable output. Needs multi-statement setup on the emulator | Medium: run each model as `CREATE TABLE AS` in order |
| 4 | Shared refactors and incremental proofs | 137 + 15 generated cases | 136/137 ready, 15/15 proven | Dedup and incremental-model changes, the second practical target. Near ceiling, so a regression floor rather than a climb | Medium, same runner as 3 |
| 5 | Jaffle Shop refactors | 8 authored refactors on an open dbt project | 4/8 proved, 7/7 refutations caught | Small but real model shapes (CTE stacks, joins) with 4 unproved | Low once 3 exists; the project is DuckDB-flavoured, so the SQL needs a BigQuery pass |
| 6 | Spider 2.0 BigQuery gold queries, mutated | 142 queries with published gold SQL (205 BigQuery and GA4 tasks exist, the rest are withheld) | none as pairs | The best source of real BigQuery shapes: GA4 nested fields, wildcard tables, QUALIFY, STRUCT and ARRAY. Single queries, so pairs must be made: rewrites KumoSQL proves (equivalent by construction) and small mutations (not equivalent, with the emulator as judge) | High: a pair generator and a schema-driven data generator for nested types |
| 7 | Real Dataform projects | 13 pinned open-source projects | none as pairs | Incremental branches, JavaScript-generated SQL, procedures. Refactor pairs have to be authored | High |
| 8 | ClickBench BigQuery adapter | 43 queries over one wide table | 3/43 rewritten (on PostgreSQL) | Native GoogleSQL workload with 40 queries left unrewritten. Licence is CC BY-NC-SA 4.0, so keep it an optional corpus read from a checkout, never committed | Medium; synthetic data for a 100-column table |

Left out on purpose: SQL-RewriteBench, Calcite, Singh, VeriEQL, QED, QUITE, DLBench, JOB (all other dialects), and the soundness fuzz (generated inside the subset where BigQuery and DuckDB agree, which this repository does not use).

Not checked: whether KumoSQL's sample-database pairs (54) and unsafe-rewrite corpus (560) are written in BigQuery or in a mix. Both would be good floors if they are native; check before porting.

## An eval for the emulator, not the prover

GoogleSQL compliance tests: 4,748 claimed cases with the typed rows Google's reference implementation returns (Apache-2.0, the pinned fixture is in KumoSQL). Running them on the emulator gives a per-construct list of where it is wrong. That does not climb the prover's score. It decides which constructs the prover may trust the emulator on, which is what keeps "0 wrong" honest. It has been run: see [evals/googlesql-compliance.md](evals/googlesql-compliance.md) and [emulator-gaps.md](emulator-gaps.md).

## What the first two baselines showed

See [evals/pairs.md](evals/pairs.md) for the scores, the emulator gaps found and the corpus problems found.

## Order of work

1. Raise numeric traps and documented rewrites. Done first: data seeded with each pair's own constants ([evals/pairs.md](evals/pairs.md)). Proof side done ([ported-layers.md](ported-layers.md)). Next: decide how to label float SUM order.
2. ~~Run the compliance fixture on the emulator and write the gap list.~~ Done: [evals/googlesql-compliance.md](evals/googlesql-compliance.md), [emulator-gaps.md](emulator-gaps.md).
3. Pipeline equivalence and refutation on the emulator.
4. Spider 2.0 pair generator.
