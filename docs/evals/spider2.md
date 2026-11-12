# Pairs from the Spider 2.0 BigQuery gold queries

**Purpose:** real BigQuery queries (GA4 nested events, wildcard tables, CTE stacks, window functions) paired with rewrites and mutations of themselves, so the prover can be improved on shapes nobody wrote for it. Spider 2.0 is a text-to-SQL benchmark; only its published reference queries are used here, not its questions or its score. The generator lives in `src/kumo_bq_prover/` (`schema_infer.py`, `nested_datagen.py`, `pairgen.py`, `spider2.py`) and is driven by `python tools/spider2_pairs.py --split dev|held-out`.

**Example:** a query that reads `events_*`, unnests `event_params`, filters `ep.key = 'page_title'` and `_TABLE_SUFFIX BETWEEN '20210101' AND '20210107'` becomes: a table `t_0` with `event_params ARRAY<STRUCT<key STRING, value STRUCT<string_value STRING>>>` and a `tsuffix_` column; random databases whose `key` values include `page_title` and whose suffixes fall inside the range; a rewrite that expands `BETWEEN` into `>=` and `<=` (equivalent by construction); and a mutation that turns `>` into `>=` (kept as `not_equivalent` only if the emulator finds a database where the rows differ).

## Source and licence

The 142 gold queries are copied unmodified from [xlang-ai/Spider2](https://github.com/xlang-ai/Spider2) (`spider2-lite/evaluation_suite/gold/sql`, MIT, Copyright (c) 2024 bird_sql) into `benchmarks/spider2/gold/`, with the licence in `benchmarks/spider2/LICENSE-Spider2` and each file's SHA-256 in `benchmarks/spider2/manifest.json`. 205 BigQuery and GA4 tasks exist; upstream publishes gold SQL for 142 only. The copy came from KumoSQL's fixture of the same files (fetched 2026-10-02); nothing was read from the public tables.

**Split:** every fifth task by SHA-1 of its id is held out (30 queries); 112 are development. It is the split KumoSQL's own Spider 2.0 eval uses. Rewrite rules, mutation operators, schema inference and the data generator were written against the development queries only. The held-out file was generated once, after the code was final. **Exposure:** two held-out queries were read while debugging: `bq001` (the first 1,500 characters) and `bq004` (the whole query). The fix `bq004` prompted (an `UNNEST` alias that has the name of its own column, `UNNEST(hits) AS hits`) is general and also occurs in development queries. Both stay in the held-out file and count as tuned on test.

## How a pair is made

1. **Prepare (`schema_infer.prepare`).** DECLAREd variables are inlined, public tables are renamed `t_0`, `t_1`, ... (date shards `events_20210101` and wildcards `events_*` share one table, and `_TABLE_SUFFIX` becomes a STRING column `tsuffix_`). Every column path the query reads is collected, including paths through `UNNEST` aliases and through CTEs and subqueries, and each gets a type from how it is used: compared to a number, passed to `LOWER`, `SUM`'d, `EXTRACT`ed. STRUCT and ARRAY columns come from `a.b.c` paths and `UNNEST(...)`. Columns the query joins or compares for equality share one value pool. A STRING column that is `CAST` or `PARSE_DATE`d remembers the format it must have. A query with a geography function, a temporary function or more than one query is reported as unsupported.
2. **Check (`spider2.choose_schema`).** The emulator analyses the query on empty tables of the inferred schema. Date-like and number-like columns carry alternatives (DATE, TIMESTAMP, DATETIME) and the first variant the emulator accepts is used. A query it rejects is "does not run", with the emulator's message, and yields no pairs.
3. **Data (`nested_datagen`).** 20 seeded random databases, 3 to 10 rows per table, arrays of 0 to 4 elements. Values come from small pools; the constants the query compares against (`harvest`) and the constants themselves (`harvest(..., exact=True)`, counted three times, plus a value inside each BETWEEN range) are put in the pools, so filters match some rows.
4. **Variants (`pairgen`).** Rewrites, each rule at all its sites, then all together: `x BETWEEN a AND b` as `x >= a AND x <= b`, `IN (...)` as `OR`s, `a <> b` as `NOT (a = b)`, `IF` and one-branch `CASE` for each other, `ELSE NULL` made explicit, `COALESCE` as `IFNULL`, `COUNT(*)` as `COUNT(1)`, operands of `=` and `AND` swapped, the query wrapped in a CTE, CTEs renamed, `SELECT DISTINCT` as `GROUP BY`. Mutations, up to 8 per query spread over operators: `>` to `>=`, `>` to `<`, `=` to `<>`, `AND` to `OR`, a dropped condition, a constant plus one, a string constant extended, INNER and LEFT JOIN swapped, `UNION ALL` to `UNION DISTINCT`, `DISTINCT` toggled, `COUNT(DISTINCT x)` to `COUNT(x)`, `MAX`/`MIN` and `SUM`/`MAX` swapped, `ORDER BY` direction flipped, `LIMIT` plus one, a negation dropped.
5. **Labels.** Every variant runs next to the original on every database. A rewrite is labelled `equivalent` (by construction) and the oracle tries to refute it: a refutation is `WRONG` and fails the run. A mutation is `not_equivalent` only if some database gives different rows **and** `trust.untrusted_reasons` allows it (see [emulator-gaps.md](../emulator-gaps.md)); its witness (the setup statements, both row sets) is stored. Otherwise it is `unlabelled`: it may be equivalent. Every pair also goes through the static proof layers (`static.prove_static_detailed`, without a schema); a proof of a pair the emulator refuted would be `FALSE_PROOF` and fail the run.

## Scores

Results: `benchmarks/results/spider2-pairs.json` (summary), `spider2-pairs-dev.json` and `spider2-pairs-held-out.json` (per query); the pairs are in `benchmarks/pairs/spider2-bigquery-dev.jsonl` and `-held-out.jsonl`. emulator v0.8.1, 20 databases per query.

| | Development | Held out |
|---|---|---|
| Queries | 112 | 30 |
| Run on the emulator | 80 | 23 |
| Of those, return rows on some database | 49 | 13 |
| Pairs | 1,031 | 311 |
| Rewrites (equivalent by construction) | 397 | 127 |
| Rewrites refuted by the oracle | 0 | 0 |
| Rewrites proved by the static layers | 111 | 57 |
| Rewrites on a query that never returns rows | 170 | 59 |
| Mutations | 634 | 184 |
| Mutations refuted, `not_equivalent` with a witness | 123 | 25 |
| Mutations whose difference the trust guard withheld | 167 | 58 |
| Mutations that agreed on every database | 308 | 96 |
| Pairs proved although the emulator refuted them | 0 | 0 |

The static layers proved rewrites mostly of the wrapper kinds (CTE rename, CTE wrap) and of operand order; on the development split, 74 of the 111 proofs are on queries that return rows. The proof layers are what this corpus is for: 286 of 397 development rewrites are still unproved, and each is equivalent by construction.

## What does not run (development)

32 queries: 8 use geography functions the emulator does not have (`ST_GEOGPOINT`, `ST_AREA`, `ST_DISTANCE`, `ST_INTERSECTS`), 1 is a script with `CREATE`, and 23 fail analysis on the emulator: 10 because a type guess is wrong (a column typed STRING that is summed, a field read on the wrong type, a UNION whose branches disagree), 4 because a column or table is missing from the guess, 4 because the emulator hits a division by zero while analysing the empty tables, 3 because the emulator cannot parse the statement, and 2 because a column name is ambiguous in a join. The reason per query, with the emulator's message, is in `spider2-pairs-dev.json`.

## Limits

- **The data are synthetic and the schemas are guesses.** A column typed `INT64` that runs may be a `STRING` in the real table. Nothing was executed on the public tables.
- **Half the runnable queries return nothing.** On 31 of 80 development queries, no database made the original return a row (a cohort needs two tables to agree on several keys, a window needs two groups, a filter needs a constant the pools did not reach). Rewrites on those agree trivially; the table counts them separately. A trial that made 45% of the cells take the query's own constants turned only 3 of the 31 into queries with rows, so it was not adopted (the recorded runs predate it); a solver for the filters would do better.
- **Rewrites are equivalent by construction, not proved.** Each is a textbook identity, but nothing here checks that the rule was applied correctly except the oracle and the static layers. A query with a `LIMIT` over tied rows can differ between two equivalent spellings; none did.
- **A mutation's label comes from the emulator.** The trust guard (`node:Between`, `node:In`, `fn:EXTRACT`, `node:Like` are the constructs that withheld most) costs 167 development refutations, which a better emulator or a real-BigQuery check would recover. `unlabelled` is not "equivalent".
- **Static proofs run without a schema** (the proof layers take flat types and these schemas are nested), so they work from the queries as written.
- Results are compared as multisets, so a difference only in row order is not seen.
