# Held-out pair set

**Purpose:** a set of labelled BigQuery pairs that the proof layers were never tuned on, so a score on it says something about pairs they have not seen. The two older corpora cannot do that: the layers' rules were written against them in the earlier prover.

**Example:** `SELECT id FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)` against the `NOT EXISTS` form is labelled different: one NULL `customer_id` makes `NOT IN` return no rows. The static layers must not prove it, and the emulator refutes it on a database with a NULL.

**The set** (`benchmarks/pairs/heldout-2026-11-12.jsonl`): 63 pairs, 39 equivalent and 24 different, over a three-table schema. They cover three-valued logic, joins and filters in `ON` against `WHERE`, `IN`/`NOT IN`/`EXISTS`, `QUALIFY` and window frames, `UNNEST`, `STRUCT`, `GROUP BY`/`HAVING`, `COUNTIF`, `SAFE_DIVIDE`, date ranges, `SELECT * EXCEPT`, set operations and integer division.

**How it was frozen:** the pairs and labels were committed (`286c528`) before the proof layers ran on them. Labels were checked on the emulator only: all 39 equivalent pairs agree on 12 seeded databases, all 24 different pairs are refuted (three of them on a witness row written into the case). The proof layers have not seen the pairs since, except to score them. Rule: do not tune on this set. A rule change motivated by one of its pairs must say so, and the set must then be replaced, not edited.

**Run:** `python tools/run_pairs_eval.py heldout-2026-11-12 --databases 8` (add `--split held-out` for the quarter of an older corpus described below).

## Score (2026-11-12, emulator v0.8.1)

| | Count |
|---|---|
| Equivalent proved | 28 of 39 (1 identical, 3 structural, 24 algebraic) |
| Equivalent not proved | 11 (5 withheld, see below) |
| Equivalent refuted | 0 |
| Different refuted | 23 of 24 (3 of these were demoted to unknown by the [trust guard](../emulator-gaps.md) until it was narrowed on 2026-11-12; the set was run once more afterwards and not tuned on) |
| False proofs | 0 |

Results file: `benchmarks/results/pairs-heldout-2026-11-12.json`.

The five withheld proofs are pairs the algebraic layer proved equal and then could not compare for runtime errors (`ho-countif`, `ho-where-to-having-key`, `ho-struct-field`, `ho-window-sum-frame`, `ho-scalar-subquery-constant`); the policy is in [ported-layers.md](../ported-layers.md). The other six unproved pairs are plain recall misses: a half-open date range against `BETWEEN`, a literal array length, a `COUNT(DISTINCT)` against a distinct subquery, a `UNION ALL` of two complementary filters, `x * 2` against `x + x`, and `ABS` against a subtraction. These are the cheapest next rules, and by the rule above each one that is added from here needs a replacement pair.

The one missed different pair, `ho-in-vs-join-duplicates`, is data the eight random databases did not draw (it was refuted in the label check on twelve).

## Older corpora, split

`tools/run_pairs_eval.py --split dev|held-out` takes every fourth case from the fourth (the convention of KumoSQL's numeric-traps bench) as held out. This is a weaker guarantee than the set above: the earlier prover's rules saw all of these cases, so the split only shows whether the cases that KumoSQL's own bench held out behave like the rest.

| Corpus | Split | Equivalent proved | Different refuted | False proofs |
|---|---|---|---|---|
| Documented rewrites | dev | 14 of 14 | 3 of 5 (1 invalid in BigQuery) | 0 |
| Documented rewrites | held-out | 4 of 5 | 1 of 1 | 0 |
| Numeric traps | dev | 40 of 50 proved, 7 under an assumption | 10 of 19 (2 more proved only under the no-NaN assumption) | 0 |
| Numeric traps | held-out | 11 of 15 proved, 1 under an assumption (1 refuted by the emulator's CAST bug) | 2 of 7 (1 more proved only under the no-NaN assumption) | 0 |

(The counts are from the 2026-11-12 runs in [pairs.md](pairs.md), cut by index.)

## Limits

- The set was written by the agent that ported the layers and knew their rules. It is held out from tuning, not independent.
- 63 pairs, one schema, no pipelines, no `MERGE`, no JavaScript UDFs, no real warehouse data.
- Labels follow GoogleSQL as its author knows it; none was run on real BigQuery. The emulator agrees with every label, and it has known gaps ([pairs.md](pairs.md)).
