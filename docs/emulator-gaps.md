# Where the emulator is wrong (GoogleSQL compliance)

**Purpose:** the oracle is the BigQuery emulator, so a counterexample is only as good as the emulator's answer. This page lists where Google's GoogleSQL compliance cases show the emulator disagreeing with the reference implementation, which of those disagreements the prover guards against, and which it does not. The scores are on [the eval page](evals/googlesql-compliance.md).

**Example:** `SELECT 3 IN (1, 2, NULL)` is NULL on real BigQuery and FALSE on the emulator. A pair that differs only in that expression would be refuted by the emulator with a counterexample BigQuery does not reproduce. The prover now answers UNKNOWN for it, naming the construct (`node:In`).

## What the guard does

`oracle.compare` still runs both queries and compares rows. When the rows differ, `trust.adjudicate` (`src/kumo_bq_prover/trust.py`) decides whether the difference is a counterexample for BigQuery or only the emulator's own mistake:

- **Constructs.** If either query uses a construct in `src/kumo_bq_prover/untrusted_constructs.json` (the table below), the answer is UNKNOWN. The file is written by `python tools/compliance_gaps.py RESULTS.json --write` from the development half of the compliance cases, plus the divergences found by hand and confirmed on real BigQuery (`CURATED` in that tool). A query that does not parse is never trusted either. A tag names the part that is wrong, not the whole function: `extract:WEEK` (the other `EXTRACT` parts are exact), `like:wild` (a literal pattern without `_` or an escape is exact), `format:unverified` (only the `FORMAT_DATE` directives checked on BigQuery are exact).
- **Conditional constructs.** `fn:SUBSTRING`, `fn:STR_POSITION` and `fn:INSTR` are untrusted only when the witness holds non-ASCII text (the emulator counts bytes there), and `like:plain` only when it holds a newline. Without a witness the cautious reading applies.
- **Values.** If the counterexample data or either result holds NaN, an infinity, a negative zero, a float or NUMERIC magnitude from 1e18, an integer from 2^62, or a NUMERIC or BIGNUMERIC literal with more than nine decimals (in the setup or the query), the answer is UNKNOWN. The compliance failures of the common constructs (SUM, ARRAY, DISTINCT) concentrate on such values.
- **Exact rewrites.** When the only untrusted constructs are `IN`, `BETWEEN` and `IS [NOT] TRUE|FALSE`, both queries are rewritten without them (`src/kumo_bq_prover/gap_rewrites.py`: `IN` as `OR` of `=`, or as a `CASE` over `EXISTS` for a subquery or `UNNEST`; `BETWEEN` as `>=` and `<=`; `IS TRUE` as `COALESCE`) and run again on the same data. If the rewritten pair still differs, that is a counterexample for BigQuery, and the stored rows are the rewritten queries' rows. If it agrees, the difference was the emulator's gap and the answer is UNKNOWN. A construct that cannot be rewritten exactly (a row-valued `IN`, a set-operation subquery, `SELECT *`, an aggregate as the left side of `IN (SELECT ...)`, a volatile function) keeps the difference withheld. The rewrite runs only to check a refutation; it proves nothing.

The guard only costs refutations. It cannot turn an unknown into a proof. `compare(..., trust=False)` reports every difference as REFUTED, which is how the cost is measured. The result keeps the emulator's rows and the reasons (`OracleResult.untrusted`).

## Confirmed on real BigQuery

Inline constants, 0 bytes billed, 2026-11-12. In each row the emulator and BigQuery return different answers.

| Query | BigQuery | Emulator |
|---|---|---|
| `SELECT 3 IN (1, 2, NULL)` | NULL | FALSE |
| `SELECT 3 NOT IN (1, 2, NULL)` | NULL | TRUE |
| `SELECT NULL IN UNNEST(['a', 'b', NULL])` | NULL | FALSE |
| `SELECT NULL IS TRUE` | FALSE | NULL |
| `SELECT 10 BETWEEN NULL AND 5` | FALSE | NULL |
| `SELECT CAST(DATE '2023-05-23' AS TIMESTAMP)` | 1684800000 (UTC midnight) | 1684825200 (Los Angeles midnight) |
| `SELECT ASCII(b'a')` | 97 | 89 |
| `SELECT b'Value1' LIKE ANY (b'%1', b'%2')` | TRUE | FALSE |
| `SELECT 'a' LIKE '_'`, `'Value1' LIKE 'Val_e1'`, `'a_b' LIKE r'a\_b'` | TRUE | FALSE |
| `SELECT 'a\nb' LIKE 'a%b'`, `'a\nb' LIKE 'a_b'` | TRUE | FALSE |
| `SELECT EXTRACT(WEEK FROM DATE '2016-01-21')` (also `2021-01-01`, `2023-01-01`, `2023-06-18`) | 3 (0, 1, 25) | 2 (52, 52, 24) |
| `SELECT DATE_DIFF(DATE '2021-01-01', DATE '2020-01-01', ISOWEEK)` | 52 | 100 |
| `SELECT SUBSTR('héllo', 3)`, `SUBSTR('日本語x', 2, 2)` | `llo`, `本語` | bytes, not characters (invalid text) |
| `SELECT STRPOS('日本語x', 'x')`, `INSTR(...)` | 4 | 10 |
| `SELECT FORMAT_DATE('%j', DATE '2016-01-21')` | `021` | `21` |
| `SELECT FORMAT_TIMESTAMP('%U', TIMESTAMP '2021-01-01 00:00:00+00')` | `00` | `53` |

Directives of `FORMAT_DATE`, `FORMAT_TIMESTAMP`, `FORMAT_DATETIME` and `FORMAT_TIME` checked on two timestamps (`%Y %m %d %H %M %S`, `%A %a %B %b %p %s %T %F %u %Z %E3S` and the others in `VERIFIED_FORMAT_DIRECTIVES` in `constructs.py`): identical text on the emulator and BigQuery. Different: `%c`, `%e` (no padding), `%g`, `%j` (no padding), `%U`, `%V` (no padding), `%W`, `%w`, `%z`, `%E*S`. Anything outside the verified set is untrusted. Also identical: `EXTRACT` of `YEAR`, `QUARTER`, `MONTH`, `DAY`, `DAYOFWEEK`, `DAYOFYEAR`, `ISOYEAR`, `ISOWEEK`, `HOUR`, `MINUTE`, `SECOND`, `MILLISECOND` and `DATE` from a DATE, TIMESTAMP or DATETIME; `DATE_TRUNC` to `WEEK`, `WEEK(MONDAY)`, `ISOWEEK`, `MONTH`, `QUARTER`, `YEAR`; `DATE_DIFF` in `DAY`, `WEEK`, `MONTH`; `LAST_DAY(..., WEEK)`; `LIKE` with `%` and a literal pattern; `LEFT`, `RIGHT`, `LENGTH`, `UPPER`, `LOWER`, `REPLACE`, `TRIM`, `LPAD`, `RPAD`, `SPLIT`, `REGEXP_*`, `REVERSE`, `STARTS_WITH`, `CONTAINS_SUBSTR` on non-ASCII text.

The `IN`, `BETWEEN` and `IS TRUE` rewrites of [`gap_rewrites.py`](../src/kumo_bq_prover/gap_rewrites.py) were checked the same way: 13 expression forms (`IN` with a list, a subquery and `UNNEST`, each also negated and correlated; `BETWEEN` and `NOT BETWEEN`; `IS TRUE`, `IS NOT TRUE`, `IS FALSE`, `IS NOT FALSE`) over 32 rows (the left value NULL, 1, 2 or 3 against every subset of NULL, 1, 2) on BigQuery. The original and the rewritten expression gave the same answer on every row, and the emulator's answer to the rewritten form is the same as BigQuery's (`tests/test_gap_rewrites.py`, expected strings in `tests/fixtures/gap_rewrites_bigquery.json`).

Found in earlier runs of the pair corpora and the [proof audit](evals/proof-audit.md), listed in [evals/pairs.md](evals/pairs.md) (`x IS NOT TRUE` returns NULL for NULL, and a `UNION ALL` branch picks up the columns of an earlier branch that repeated an expression, also checked on real BigQuery): `CAST(FLOAT64 AS INT64)` truncates where BigQuery rounds half away from zero, NaN reads back as NULL, -0.0 as 0.0, `LEFT JOIN UNNEST` drops the row of an empty array, unary minus is missing, `1e309` and `CAST('NaN' AS FLOAT64)` fail to format. `SELECT SAFE_DIVIDE(1, 0)` agrees (NULL on both); only the overflow case `SAFE_DIVIDE(1e300, 1e-300)` differs.

A table named `jobs` can be created on the emulator but cannot be read or written afterwards (`Column job_id is not present in table jobs`, found 2026-11-12 on the Oracle HR sample pairs). The oracle answers UNKNOWN through the emulator error; nine pairs of [evals/sample-databases.md](evals/sample-databases.md) are affected. Real BigQuery has no such restriction. Not found by the compliance cases, which never name a table `jobs`.

## Gaps by class

Counts are over the 4,748 claimed cases (3,149 development, 1,599 held-out); see the eval page for every figure.

**Wrong results (196 cases).** The emulator returns rows, other than the reference's:
- three-valued logic with NULL: `IN`/`NOT IN` with a NULL element or an array holding NULL, `IS TRUE`/`IS FALSE` on NULL, `BETWEEN` with a NULL bound, struct equality with a NULL field;
- bytes: `ASCII`, `LIKE ANY`/`LIKE ALL` on bytes, `SPLIT` with an empty delimiter, `CAST` to bytes, `SUBSTRING`, `TRIM`, `SPLIT_SUBSTR`, regular expression functions;
- NUMERIC and BIGNUMERIC: casts and literals, `AVG` and `SUM` near the range limits, standard deviation and variance at high precision (the emulator returns 0), BIGNUMERIC rounding;
- float extremes: window `SUM`, `STDDEV`, `CORR` and `COVAR` near 1e308 return infinity or the wrong sign;
- intervals: arithmetic and casts;
- dates and times: `CAST(DATE AS TIMESTAMP)` uses the Los Angeles zone, `EXTRACT`, `FORMAT`, `GENERATE_*_ARRAY`;
- string aggregation and min/max windows over arrays, `LAG`/`LEAD` defaults.

**Errors not raised (49 cases).** The reference raises `out_of_range` or `invalid_argument` and the emulator returns rows: float and integer overflow in `SUM` and `/`, `CAST` of an out-of-range float to INT64, `LAG`/`LEAD` with a negative or NULL offset or a non-constant default, a scalar subquery that returns two rows, `ARRAY_AGG ... LIMIT` with an invalid limit. The guard does not cover these; see the residual risk below.

**Rejected by the emulator (1,095 cases).** Already UNKNOWN in the oracle: `CORRESPONDING` and `BY NAME` set operations (661), `LIKE ANY|ALL` with an array of patterns, `GROUP BY ALL`, window frame offsets that are parameters, `ARRAY_AGG` with NULL inputs, interval addition, grouping sets over arrays, nil pointer dereferences inside the emulator.

**Tables the emulator cannot build (853 cases).** Cases are not scored when a table they read cannot be created or does not read back as printed: tables holding NaN or infinity (the emulator cannot parse them: `ParseFloat` and `ParseInt` failures, 552), GoogleSQL-only enum and protocol buffer types (155), query parameters the runner cannot pass (83: a literal the emulator cannot format, or an array or struct value), and a few others. Every aggregate over NaN or infinity data is therefore unmeasured, not trusted.

**Emulator crashes (5 cases).** The emulator process died or hung on the case. The runner restarts it and records the case.

**Time zones (41 cases).** The reference ran with its default zone, America/Los_Angeles; BigQuery's default is UTC and the emulator cannot set one (`@@time_zone` reads NULL). These cases are set aside, not counted as failures.

## Untrusted constructs

Written by `tools/compliance_gaps.py` from the development files. A construct is untrusted when at least a quarter of the compliance cases that use it (and at least two) disagree with the reference, or when it is in the hand-kept list `CURATED` in that tool (findings on real BigQuery and volatile functions). A tag names the part that is wrong: tags checked on BigQuery as exact (`extract:YEAR` and the other parts but `WEEK`, `format:verified`) are never blamed for a failure they only share a case with (`CHECKED_EXACT`). Entries marked "only when" are conditional (`CONDITIONAL`). Constructs in more than a third of all cases (`lit:int`, `node:Window`) never are. Rows with no note are untrusted on the compliance evidence alone; `cast_lit:T` is a cast of a string literal (`TIMESTAMP '2020-01-01'` parses as one); `cast_rounded:INT64` (a cast of a whole `ROUND(x)` or an integer literal) is exact and absent.

| Construct | Compliance cases using it that pass | disagree | rejected | Note |
|---|---:|---:|---:|---|
| `node:ByteString` | 83 | 30 | 3 |  |
| `node:In` | 111 | 14 | 17 | IN and NOT IN with a NULL element return FALSE or TRUE where the reference returns NULL |
| `cast:NUMERIC` | 17 | 13 | 3 |  |
| `like:wild` | 14 | 12 | 16 | LIKE with `_`, an escape or a pattern that is not a literal: `'a' LIKE '_'` is FALSE on the emulator |
| `cast_lit:BIGNUMERIC` | 10 | 10 | 3 |  |
| `cast_lit:NUMERIC` | 9 | 9 | 7 |  |
| `fn:CORR` | 24 | 9 | 6 |  |
| `fn:SAFE_FUNC` | 21 | 8 | 3 |  |
| `node:Interval` | 6 | 8 | 20 |  |
| `node:Any` | 8 | 7 | 16 |  |
| `fn:SPLIT_SUBSTR` | 4 | 5 | 0 |  |
| `node:All` | 5 | 5 | 0 |  |
| `cast:BYTES` | 9 | 4 | 0 |  |
| `cast:STRUCT` | 7 | 4 | 1 |  |
| `fn:PERCENTILE_CONT` | 3 | 4 | 7 |  |
| `fn:SUBSTRING` | 5 | 4 | 0 | SUBSTR counts bytes, not characters, in non-ASCII text on the emulator (untrusted only when the witness holds non-ASCII text) |
| `node:HavingMax` | 6 | 4 | 2 |  |
| `type:STRING` | 12 | 4 | 4 |  |
| `type:YEAR TO SECOND` | 0 | 4 | 3 |  |
| `cast_lit:FLOAT64` | 2 | 3 | 61 |  |
| `extract:MICROSECOND` | 0 | 3 | 0 |  |
| `fn:DATE_FROM_UNIX_DATE` | 0 | 3 | 0 |  |
| `fn:FORMAT` | 7 | 3 | 1 |  |
| `fn:RAND` | 3 | 3 | 0 |  |
| `fn:REGEXP_INSTR` | 0 | 3 | 0 |  |
| `fn:REGEXP_MATCH` | 0 | 3 | 0 |  |
| `fn:TRIM` | 5 | 3 | 0 |  |
| `join:LEFT:UNNEST` | 3 | 3 | 0 | LEFT JOIN UNNEST drops the row of an empty array on the emulator |
| `type:STRUCT` | 5 | 3 | 0 |  |
| `cast:INT64` | 20 | 2 | 58 | CAST(FLOAT64 AS INT64) truncates on the emulator; BigQuery rounds half away from zero |
| `cast_lit:DATETIME` | 4 | 2 | 1 |  |
| `fn:ASCII` | 1 | 2 | 0 |  |
| `fn:GENERATE_DATE_ARRAY` | 2 | 2 | 0 |  |
| `fn:GENERATE_SERIES` | 1 | 2 | 0 |  |
| `fn:GENERATE_TIMESTAMP_ARRAY` | 0 | 2 | 0 |  |
| `fn:JSON_EXTRACT` | 6 | 2 | 0 |  |
| `fn:JSON_FORMAT` | 0 | 2 | 0 |  |
| `fn:REGEXP_EXTRACT` | 1 | 2 | 0 |  |
| `fn:REGEXP_EXTRACT_ALL` | 1 | 2 | 1 |  |
| `is:boolean` | 4 | 2 | 0 | NULL IS TRUE returns NULL where the reference returns FALSE |
| `node:RawString` | 1 | 2 | 1 |  |
| `fn:CURRENT_DATE` | 2 | 1 | 1 | the two queries run at different moments, so they can differ without being inequivalent |
| `fn:IEEE_DIVIDE` | 3 | 1 | 9 | NaN and infinity results cannot be held (NaN reads back as NULL) |
| `fn:JUSTIFY_DAYS` | 0 | 1 | 0 | EXTRACT from the result of an interval function differs on the emulator |
| `fn:JUSTIFY_HOURS` | 0 | 1 | 0 | EXTRACT from the result of an interval function differs on the emulator |
| `fn:JUSTIFY_INTERVAL` | 0 | 1 | 0 | EXTRACT from the result of an interval function differs on the emulator |
| `fn:SAFE_DIVIDE` | 8 | 1 | 0 | SAFE_DIVIDE(1e300, 1e-300) returns infinity where the reference returns NULL (float overflow) |
| `fn:STR_POSITION` | 3 | 1 | 0 | STRPOS counts bytes, not characters, in non-ASCII text on the emulator (untrusted only when the witness holds non-ASCII text) |
| `like:plain` | 8 | 1 | 0 | a LIKE pattern with % does not match across a newline on the emulator, and `_` does not match one (untrusted only when the witness holds a newline) |
| `cast:TIMESTAMP` | 1 | 0 | 0 | CAST(DATE AS TIMESTAMP) applies the America/Los_Angeles zone on the emulator; BigQuery uses UTC (checked on BigQuery) |
| `date_diff:ISOWEEK` | 0 | 0 | 0 | DATE_DIFF(..., ISOWEEK) counts the wrong number of ISO weeks on the emulator |
| `date_diff:WEEKDAY` | 0 | 0 | 0 | DATE_DIFF(..., WEEK(<weekday>)) was not checked on BigQuery |
| `extract:WEEK` | 0 | 0 | 0 | EXTRACT(WEEK ...) numbers weeks differently (2016-01-21 is week 3 on BigQuery, 2 on the emulator) |
| `fn:CURRENT_DATETIME` | 0 | 0 | 0 | the two queries run at different moments, so they can differ without being inequivalent |
| `fn:CURRENT_TIME` | 0 | 0 | 0 | the two queries run at different moments, so they can differ without being inequivalent |
| `fn:CURRENT_TIMESTAMP` | 0 | 0 | 0 | the two queries run at different moments, so they can differ without being inequivalent |
| `fn:INSTR` | 0 | 0 | 0 | INSTR counts bytes, not characters, in non-ASCII text on the emulator (untrusted only when the witness holds non-ASCII text) |
| `fn:IS_INF` | 0 | 0 | 0 | the emulator cannot hold infinities except through IEEE_DIVIDE |
| `fn:IS_NAN` | 0 | 0 | 0 | the emulator reads NaN back as NULL |
| `fn:MAKE_INTERVAL` | 2 | 0 | 0 | intervals differ on the emulator |
| `fn:UUID` | 0 | 0 | 0 | GENERATE_UUID differs on every call |
| `format:unverified` | 0 | 0 | 0 | FORMAT_DATE / FORMAT_TIMESTAMP with a directive not checked on BigQuery (%j and %e do not pad, %U %W %w %g %z %E*S differ) or a format that is not a literal |
| `node:AtTimeZone` | 0 | 0 | 0 | time zone conversions were not checked on BigQuery (the emulator cannot set a zone and rejects no invalid one) |
| `node:Between` | 1 | 0 | 0 | x BETWEEN NULL AND y returns NULL where BigQuery returns FALSE when the other bound already fails (found on the held-out files, so tuned on test) |
| `safe_cast:INT64` | 0 | 0 | 0 | SAFE_CAST(FLOAT64 AS INT64) truncates on the emulator; BigQuery rounds half away from zero |

## Residual risk

- The guard misses 19 of 60 failing held-out cases (and 35 of 185 development cases). They include `GROUP BY ROLLUP`/`CUBE` with a recursive query, `LIMIT` with a negative parameter, timestamp-to-time casts, `BIGNUMERIC` `AVG` and NUMERIC values printed without a `NUMERIC` literal. A refutation on those is trusted.
- **Errors not raised:** when the emulator returns rows where BigQuery raises an error, a pair can differ on the emulator and agree in BigQuery only if both queries raise there, which needs an overflow or a division by zero on both sides. The value guard catches the overflow cases built from large numbers; a division by zero in generated data is not caught.
- Constructs the compliance cases never use have no evidence at all. They are neither trusted nor untrusted; a refutation on them stands on the emulator alone.
- Expected results come from Google's reference implementation, not from BigQuery. The rows of the table above and the classes listed under it were checked on BigQuery; the other classes were not.
- **Narrowing trades recall of the guard for refutations.** A tag that names only the wrong part (`extract:WEEK`, `like:wild`) trusts every other use of the construct on the strength of the BigQuery checks listed above, which covered a few dates, timestamps and strings, not every value. A refutation through `EXTRACT`, `LIKE`, `FORMAT_DATE`, `IN` or `BETWEEN` that does not hold on BigQuery would be a false refutation; the replays of nine such witnesses on BigQuery (see [evals/spider2.md](evals/spider2.md#the-trust-guard-narrowed)) found none, and 0 of 524 rewrites were refuted on the Spider corpus.
- **The rewrites assume the emulator is right on plain operators.** `=`, `AND`, `OR`, `NOT`, `COALESCE`, `CASE`, `EXISTS` and `ROUND` were checked on NULL combinations and on the half-way values of `ROUND`, not on every type. `STRUCT`-valued and row-valued operands are left alone only when visible in the query text; a `STRUCT` column on the left of `IN` is not detected.
- `CAST(x AS INT64)` is re-checked as `CAST(ROUND(x) AS INT64)`, which is exact for FLOAT64 and NUMERIC; for an INT64 operand it goes through FLOAT64, so integers from 2^53 are refused.
