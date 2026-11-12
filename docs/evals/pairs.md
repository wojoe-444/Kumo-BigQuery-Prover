# Labelled pair corpora on the emulator

**Purpose:** score how often the emulator oracle tells equivalent pairs from different ones on pairs written in BigQuery, and find where the emulator or the labels are wrong. These are the first two hillclimb evals (see [hillclimb-evals.md](../hillclimb-evals.md)).

**Example:** `SELECT CAST(f AS INT64) FROM t` against `SELECT CAST(ROUND(f) AS INT64) FROM t` is labelled equivalent. On a database where `f` is 2.5 the emulator returns 2 for the left query and 3 for the right, so the oracle refutes an equivalent pair. Real BigQuery returns 3 for both, so the emulator is wrong, not the label.

**Corpora** (`benchmarks/pairs/`, copied from KumoSQL fixtures, which its threads authored): `documented-rewrites.jsonl` (25 pairs, rewrites that documentation recommends) and `numeric-traps.jsonl` (112 pairs, BigQuery numeric and coercion traps). Both are development sets with no held-out split; they were written before this prover existed, but their rules were tuned on them in the earlier prover.

**Run:** `python tools/run_pairs_eval.py <corpus> --databases 8`. Each pair runs on 8 seeded random databases (and the pair's own witness rows when it has them). The emulator is restarted every 6 pairs because it slows down as tables accumulate.

## Scores (emulator v0.8.1, 8 databases per pair)

The default run seeds the databases with the constants found in each pair's queries (see [Literal harvesting](#literal-harvesting)). `--no-harvest` gives the first baseline, `--specials` adds the edge values described below.

| Corpus and run | Equivalent: not refuted | Equivalent: refuted | Equivalent: emulator error | Different: refuted | Different: missed | Different: emulator error |
|---|---|---|---|---|---|---|
| Documented rewrites (19 + 6), baseline | 19 | 0 | 0 | 1 | 4 | 1 |
| Documented rewrites, harvesting | 19 | 0 | 0 | 4 | 1 | 1 |
| Numeric traps (65 + 26, 21 unscored), baseline | 61 | 1 | 3 | 11 | 14 | 1 |
| Numeric traps, harvesting | 61 | 1 | 3 | 12 | 13 | 1 |
| Numeric traps, harvesting and `--specials` (tuned on test) | 60 | 2 | 3 | 13 | 12 | 1 |
| Documented rewrites, harvesting, with the guard | 19 | 0 | 0 | 2 (and 2 demoted) | 1 | 1 |
| Numeric traps, harvesting, with the guard | 62 | 0 | 3 | 11 (and 1 demoted) | 13 | 1 |

Nothing is proved yet: the static layer only recognises identical queries. These are refutation scores, results files `benchmarks/results/pairs-*.json`. The last two rows are the current run (see [The guard](#the-guard)); the rows above it predate the guard.

## What the runs found

Checked on real BigQuery (inline values, 0 bytes billed) unless noted.

Emulator problems:
- **Wrong result:** `CAST(FLOAT64 AS INT64)` truncates toward zero on the emulator (2.5 gives 2, 2.7 gives 2, -2.7 gives -2), where BigQuery rounds half away from zero (2.5 gives 3, 2.7 gives 3). This is the dangerous kind: it refutes a true equivalence and hides a real difference.
- **Wrong result:** NaN reads back as NULL, and -0.0 as 0.0.
- `1e309` as a literal makes the emulator fail while formatting the query; BigQuery accepts it. `CAST('NaN' AS FLOAT64)` and `CAST('inf' AS FLOAT64)` in an INSERT fail the same way; `IEEE_DIVIDE(1, 0)` works for an infinity.
- Integer literals past the INT64 range, like -9223372036854775809, are refused at analysis (as BigQuery does); a pool value must stay inside the range.
- Unary minus (`-x`) is missing.
- `LEFT JOIN UNNEST(arr)` without `ON` drops rows with an empty array (found in the first run, see [bq-shapes.md](bq-shapes.md)).

Corpus problems (the emulator agrees with BigQuery, the labels do not):
- `R012b-09` uses `qty = NULL`, which BigQuery refuses to compile. The case is not valid BigQuery.
- `numeric-literal-rounding` and `numeric-literal-below-half` use NUMERIC literals with ten decimals, which BigQuery rejects. Rounding happens in casts, not literals.

Each of these cases carries a `bigquery_note` in its record.

## Literal harvesting

`src/kumo_bq_prover/harvest.py` reads the two queries (never the label), finds comparisons between one column and constants (`=`, `<>`, `<`, `BETWEEN`, `IN`, `LIKE`, also through `CAST` and `SUBSTR`), and offers the column the constant and its neighbours: integers plus and minus one, strings with and without a `%` and a trailing digit, dates plus and minus a day, timestamps at both ends of a date and one second either side. `datagen.random_database(extra=...)` draws from those values 60% of the time for the column. It only adds values, so it can find more counterexamples but cannot make two different queries look alike. NUMERIC constants with more than nine decimals are not offered (BigQuery rejects them).

It moved documented rewrites from 1 to 4 refuted and numeric traps from 11 to 12. The three documented refutations were read by hand: an order at 23:59:58 on the last day of a year that only the BETWEEN version counts, a product added before the date that drops out of a join, and a code `hn-` in lower case that the `SUBSTR = 'HN-'` test does not match but a LIKE on the name does (a real difference, though not the one the case's note describes).

`--specials` adds infinities, 1e16 and NUMERIC values at the scale limit to the base pools. That list was chosen after reading the misses, so its numbers are tuned on test. It gained one refutation (`numeric-double-round`, via 0.045) and one extra "wrong": `float-sum-union-branches-swapped`, labelled equivalent, differs because 2.5 + -1 + 1e16 and 1e16 + 2.5 + -1 round differently. BigQuery documents floating-point SUM as order-dependent, so the label needs a decision (a tolerance, or marking such pairs), not the prover.

**Remaining misses (12 with specials):** five need NaN or -0.0 in the data, which the emulator cannot hold (NaN reads back as NULL, -0.0 as 0.0, and `CAST('NaN' AS FLOAT64)` in an INSERT fails to format). Two (`cast-versus-trunc`, `div-versus-cast`) hide behind the emulator's truncating CAST. Three need two NUMERIC columns at 0.000000001 in one row, which a random draw rarely gives. The last two need a float-sum regrouping or a NUMERIC beyond float range.

**Limits:** results are compared as multisets, so a pair that differs only in row order counts as agreeing. Both queries raising an error on the same database counts as an error, not an agreement. A refutation is only as good as the emulator's answer on that construct, which is why every WRONG is looked at by hand before anything else.

## The guard

Since the [GoogleSQL compliance run](googlesql-compliance.md) the oracle reports UNKNOWN, not REFUTED, when two queries differ on a construct or a value the emulator is known to get wrong ([../emulator-gaps.md](../emulator-gaps.md)). `run_pairs_eval.py` counts such a pair as **demoted** (refuted on the emulator but not trusted); an equivalent pair the emulator wrongly refutes and the guard demotes is counted `ok`.

What changed in the scores (8 databases per pair, harvesting on, no specials):
- **Documented rewrites:** 4 refuted became 2 refuted and 2 demoted. `R012-037` is demoted because it uses `BETWEEN` (NULL bounds are a measured gap), `R012-042` because it uses `SUBSTRING` and `LIKE`. Both were real differences when read by hand; the guard cannot tell, so it does not report them.
- **Numeric traps:** 12 refuted became 11 refuted and 1 demoted (`safe-divide-versus-filter`, `SAFE_DIVIDE` with a result past 1e18). The one equivalent pair the emulator refuted, `cast-versus-round` (`CAST(FLOAT64 AS INT64)` truncates), is now demoted, so the corpus has 0 wrong again. `cast-versus-round` is demoted because `CAST(... AS INT64)` is on the untrusted list.
- **Data generator:** the base FLOAT64 pool no longer holds 1e308; databases with odd seeds add it (`extremes=True`). A refutation that rests on 1e308 is demoted by the value check, and half the databases are free of it so an ordinary counterexample can still be found. This change and the guard moved the scores together; the table above does not separate them.

The loss is the price of a refutation BigQuery would not reproduce: a demoted pair stays UNKNOWN until the emulator is fixed or the construct is proved some other way.
