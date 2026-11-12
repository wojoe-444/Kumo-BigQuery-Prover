# GoogleSQL compliance on the emulator

**Purpose:** measure where the BigQuery emulator, the prover's oracle, disagrees with Google's reference implementation, so the prover can answer UNKNOWN where the emulator cannot be trusted. This scores the emulator, not the prover. The gap list and the guard it feeds are in [../emulator-gaps.md](../emulator-gaps.md).

**Example:** the case `SELECT 3 IN (1, 2, NULL)` expects `NULL`. The emulator returns `FALSE`, so the case counts as `wrong`, and `IN` becomes an untrusted construct.

**Source** (`benchmarks/googlesql_conformance/`, copied from KumoSQL's fixture of the same name, Apache-2.0, [LICENSE](../../benchmarks/googlesql_conformance/LICENSE), [NOTICE](../../benchmarks/googlesql_conformance/NOTICE)): every case of the `googlesql/compliance/testdata/*.test` files of google/googlesql at commit `d82db99a923a46571f543a13899dc791a7b6f743`, split by file into development and held-out files by a hash of the file name (a quarter held out). A case is a query, the tables it reads (printed rows of the file's `CREATE TABLE` blocks) and the result the reference returns. The claimed subset is the one KumoSQL's GoogleSQL conformance eval fixed before its evaluator existed: queries using only features BigQuery has, without GoogleSQL-only types or topics (protos, graphs, JSON functions, ranges, maps, DML). It is 4,748 cases: 3,149 in the 94 development files and 1,599 in the 30 held-out files.

**Run:**

```sh
python tools/run_compliance.py                      # development files, summary (about 2.5 minutes, 3 emulators)
python tools/run_compliance.py --split all --json RESULTS.json
python tools/run_compliance.py --file 'strings' --show 5
python tools/compliance_gaps.py RESULTS.json --heldout RESULTS.json     # gap list and guard coverage (RESULTS holds both splits)
```

## How a case is scored

The tables are built on the emulator from the file's own setup SQL, then read back and compared with the printed rows. A table that cannot be built, or reads back differently, makes every case that reads it `fixture` (unscored). The query then runs on the emulator and the answer is compared with the reference's:

| Outcome | Meaning |
|---|---|
| pass | the same column types and rows (floats within 4 ULPs, a NULL array and an empty array are the same, signed zero is not scored, `unknown order` rows compare as multisets), or both sides raise an error |
| wrong | other rows or other column types |
| missing_error | the reference raises an error, the emulator returns rows |
| emulator_error | the reference returns rows, the emulator rejects or fails the query (already UNKNOWN in the oracle) |
| fixture | a table or query parameter could not be built on the emulator |
| zone | the expected values assume the reference driver's default time zone (America/Los_Angeles); BigQuery's default is UTC and the emulator cannot set one |
| unscored | the expected result has sub-microsecond digits BigQuery has no type for, or GoogleSQL-only number types (INT32, UINT32, UINT64, FLOAT32) |
| crash | the emulator died or hung on the case |

Only `wrong` and `missing_error` can make the oracle refute a pair for a reason BigQuery does not share. Real BigQuery was not used to score the cases: the expected values are the reference's. The eight classes in [emulator-gaps.md](../emulator-gaps.md#confirmed-on-real-bigquery) were checked on BigQuery with inline constants.

## Scores (2026-11-12, emulator v0.8.1)

| Split | Claimed | pass | wrong | missing_error | emulator_error | fixture | zone | unscored | crash |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Development files | 3,149 | 1,680 | 145 | 40 | 684 | 489 | 39 | 67 | 5 |
| Held-out files | 1,599 | 550 | 51 | 9 | 411 | 364 | 2 | 212 | 0 |

Of the 2,475 cases that ran and gave an answer that can be compared (pass, wrong, missing_error), 2,230 pass (90.1%). That figure counts only cases the emulator could take; the 1,095 it rejects and the 853 whose tables it cannot hold are not in it, so it is not a measure of how much of GoogleSQL the emulator supports.

## The guard measured

The untrusted construct list is built from the development files only. On the held-out files, whose failing cases it never saw:

| | Failing cases flagged | Passing cases also flagged |
|---|---:|---:|
| Development files (the list was built on them) | 149 of 185 | 430 of 1,680 |
| Held-out files, list as built from development | 37 of 60 | 111 of 550 |
| Held-out files, after adding `BETWEEN` (tuned on test) | 39 of 60 | 124 of 550 |
| Development files, finer tags and conditional constructs (2026-11-12) | 150 of 185 | 419 of 1,680 |
| Held-out files, finer tags and conditional constructs (tuned on test for `EXTRACT`, `LIKE`, `SUBSTR`) | 41 of 60 | 124 of 550 |

The last two rows count the guard's static part only: a case that uses `IN`, `BETWEEN`, `IS TRUE` or `CAST` to INT64 is still flagged here, although `trust.adjudicate` may let a pair through after the exact rewrite check ([emulator-gaps.md](../emulator-gaps.md#what-the-guard-does)). They show that splitting `EXTRACT`, `LIKE`, `SUBSTR` and `FORMAT` by the part that is wrong did not lose failing cases: 150 and 41 flagged against 149 and 39 before. `CURRENT_*`, `GENERATE_UUID` and `AT TIME ZONE` are untrusted now (two held-out cases that `EXTRACT` used to catch by accident).

A case is flagged when it uses an untrusted construct or its SQL or the tables it reads hold a value the value check refuses (see [emulator-gaps.md](../emulator-gaps.md#what-the-guard-does)). Compliance cases are built to probe edges, so the share of passing cases flagged here overstates what ordinary queries lose; on the two pair corpora see [pairs.md](pairs.md#the-guard).

## Splits and exposure

The harness rules were fixed on development files: time zone cases set aside, a NULL array equal to an empty one, signed zero not scored, narrow GoogleSQL number types unscored, tables checked against their printed rows without row order. The held-out files were run after that and the first figures were 550 pass, 52 wrong, 9 missing_error, 1 zone. One later change, setting aside cases whose only difference is a TIMESTAMP off by a time zone offset (a multiple of 15 minutes, at most 14 hours), was designed on development cases and applied to both splits; it moved 4 development cases and 1 held-out case from wrong to zone, which is why the table says 51 wrong and 2 zone. The `BETWEEN` entry was added after reading the held-out failures, so the last row above is tuned on test. The untrusted list in the repository is the development-derived one plus the curated entries (`CURATED` and `CONDITIONAL` in `tools/compliance_gaps.py`): `BETWEEN`, and since 2026-11-12 the parts found wrong on BigQuery (`EXTRACT(WEEK)`, `LIKE` with `_`, `DATE_DIFF` in ISO weeks, unverified `FORMAT_DATE` directives), volatile functions and time zones. The held-out failures of `EXTRACT`, `LIKE` and `SUBSTR` were read while doing that, so those tags are tuned on test.

## Limits

- The reference is Google's evaluator, not BigQuery. They can differ, and only the classes in the table of confirmed queries were checked on BigQuery.
- Cases that touch NaN or infinity data are unmeasured (the emulator cannot build the tables), and those are the values most likely to expose a gap.
- The blame is per construct, not per cause. A construct such as `cast_lit:NUMERIC` is untrusted because many of its cases fail, but the failing cases come from specific values (high precision, range limits) that the value check also catches.
- The guard misses 19 of 60 failing held-out cases (and 35 of 185 development ones), and constructs the cases never use have no evidence.
- The emulator was restarted per file group and the runner never touches real BigQuery.
