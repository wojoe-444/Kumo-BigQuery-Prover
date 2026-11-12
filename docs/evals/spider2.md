# Pairs from the Spider 2.0 BigQuery gold queries

**Purpose:** real BigQuery queries (GA4 nested events, wildcard tables, CTE stacks, window functions) paired with rewrites and mutations of themselves, so the prover can be improved on shapes nobody wrote for it. Spider 2.0 is a text-to-SQL benchmark; only its published reference queries are used here, not its questions or its score. The generator lives in `src/kumo_bq_prover/` (`schema_infer.py`, `nested_datagen.py`, `filter_data.py`, `pairgen.py`, `spider2.py`) and is driven by `python tools/spider2_pairs.py --split dev|held-out`.

**Example:** a query that reads `events_*`, unnests `event_params`, filters `ep.key = 'page_title'` and `_TABLE_SUFFIX BETWEEN '20210101' AND '20210107'` becomes: a table `t_0` with `event_params ARRAY<STRUCT<key STRING, value STRUCT<string_value STRING>>>` and a `tsuffix_` column; random databases whose `key` values include `page_title` and whose suffixes fall inside the range; a rewrite that expands `BETWEEN` into `>=` and `<=` (equivalent by construction); and a mutation that turns `>` into `>=` (kept as `not_equivalent` only if the emulator finds a database where the rows differ).

## Source and licence

The 142 gold queries are copied unmodified from [xlang-ai/Spider2](https://github.com/xlang-ai/Spider2) (`spider2-lite/evaluation_suite/gold/sql`, MIT, Copyright (c) 2024 bird_sql) into `benchmarks/spider2/gold/`, with the licence in `benchmarks/spider2/LICENSE-Spider2` and each file's SHA-256 in `benchmarks/spider2/manifest.json`. 205 BigQuery and GA4 tasks exist; upstream publishes gold SQL for 142 only. The copy came from KumoSQL's fixture of the same files (fetched 2026-10-02); nothing was read from the public tables.

**Split:** every fifth task by SHA-1 of its id is held out (30 queries); 112 are development. It is the split KumoSQL's own Spider 2.0 eval uses. Rewrite rules, mutation operators, schema inference and the data generator were written against the development queries only. The held-out file was generated once, after the code was final. **Exposure:** two held-out queries were read while debugging: `bq001` (the first 1,500 characters) and `bq004` (the whole query). The fix `bq004` prompted (an `UNNEST` alias that has the name of its own column, `UNNEST(hits) AS hits`) is general and also occurs in development queries. Both stay in the held-out file and count as tuned on test.

## How a pair is made

1. **Prepare (`schema_infer.prepare`).** DECLAREd variables are inlined, public tables are renamed `t_0`, `t_1`, ... (date shards `events_20210101` and wildcards `events_*` share one table, and `_TABLE_SUFFIX` becomes a STRING column `tsuffix_`). Every column path the query reads is collected, including paths through `UNNEST` aliases and through CTEs and subqueries, and each gets a type from how it is used: compared to a number, passed to `LOWER`, `SUM`'d, `EXTRACT`ed. STRUCT and ARRAY columns come from `a.b.c` paths and `UNNEST(...)`. Columns the query joins or compares for equality share one value pool. A STRING column that is `CAST` or `PARSE_DATE`d remembers the format it must have. A query with a geography function, a temporary function or more than one query is reported as unsupported.
2. **Check (`spider2.choose_schema`).** The emulator analyses the query on empty tables of the inferred schema. Date-like and number-like columns carry alternatives (DATE, TIMESTAMP, DATETIME) and the first variant the emulator accepts is used. A query it rejects is "does not run", with the emulator's message, and yields no pairs.
3. **Data (`nested_datagen`, `filter_data`).** 20 seeded databases per query. Three in four are built by `filter_data` ([below](#data-that-satisfies-the-querys-own-filters)): rows that satisfy the query's own filters, joins and UNNESTs, next to a few random rows. One in four is plain random (`--filtered-share` sets the split): 3 to 10 rows per table, arrays of 0 to 4 elements, values from small pools into which the constants the query compares against (`harvest`) and the constants themselves (`harvest(..., exact=True)`, counted three times, plus a value inside each BETWEEN range) are put. The random ones keep a variety the built ones lack: a NULL where no filter expected one, a value one off a boundary.
4. **Variants (`pairgen`).** Rewrites, each rule at all its sites, then all together: `x BETWEEN a AND b` as `x >= a AND x <= b`, `IN (...)` as `OR`s, `a <> b` as `NOT (a = b)`, `IF` and one-branch `CASE` for each other, `ELSE NULL` made explicit, `COALESCE` as `IFNULL`, `COUNT(*)` as `COUNT(1)`, operands of `=` and `AND` swapped, the query wrapped in a CTE, CTEs renamed, `SELECT DISTINCT` as `GROUP BY`. Mutations, up to 8 per query spread over operators: `>` to `>=`, `>` to `<`, `=` to `<>`, `AND` to `OR`, a dropped condition, a constant plus one, a string constant extended, INNER and LEFT JOIN swapped, `UNION ALL` to `UNION DISTINCT`, `DISTINCT` toggled, `COUNT(DISTINCT x)` to `COUNT(x)`, `MAX`/`MIN` and `SUM`/`MAX` swapped, `ORDER BY` direction flipped, `LIMIT` plus one, a negation dropped.
5. **Labels.** Every variant runs next to the original on every database. A rewrite is labelled `equivalent` (by construction) and the oracle tries to refute it: a refutation is `WRONG` and fails the run. A mutation is `not_equivalent` only if some database gives different rows **and** `trust.untrusted_reasons` allows it (see [emulator-gaps.md](../emulator-gaps.md)); its witness (the setup statements, both row sets) is stored. Otherwise it is `unlabelled`: it may be equivalent. Every pair also goes through the static proof layers (`static.prove_static_detailed`, without a schema); a proof of a pair the emulator refuted would be `FALSE_PROOF` and fail the run.

## Data that satisfies the query's own filters

**Purpose:** on random rows most real analytic queries return nothing, because a row has to pass several filters together, two tables have to agree on a key, an unnested array has to hold the right element, and a `HAVING COUNT(*) > 10` needs eleven rows in a group. A rewrite of a query that returns nothing agrees with it trivially, so the pair says nothing about the rewrite or about a proof of it. `filter_data.py` reads the query and builds rows it can return.

**Example (synthetic):** two cohorts of one table, joined on a user, with a group size:

```sql
WITH opened AS (SELECT user_id FROM events WHERE kind = 'open'  AND EXTRACT(YEAR FROM day) = 2022),
     closed AS (SELECT user_id FROM events WHERE kind = 'close' AND EXTRACT(YEAR FROM day) = 2023)
SELECT o.user_id, COUNT(*) FROM opened AS o JOIN closed AS c ON o.user_id = c.user_id
GROUP BY o.user_id HAVING COUNT(*) > 3
```

The builder sees two readings of `events`, so two kinds of row: one with `kind = 'open'` and a day in 2022, one with `kind = 'close'` and a day in 2023. It gives both the same `user_id` (the join links them) and repeats each row so the group reaches four. Random rows would need about eight coincidences at once.

**How it works** (`filter_data.plan`, then `FilterPlan.database` per seed):

1. **Anchors.** Every table occurrence in the query is an anchor, and so is every `UNNEST` (an element of its parent's array). A CTE or derived table read twice is two instances, so `s AS a` and `s AS b` can hold different rows. A scalar field of an anchor is a variable.
2. **Atoms.** The conjuncts of every WHERE, JOIN ... ON and non-aggregate HAVING become atoms over variables. A column read through a CTE is followed back to its anchor; an output that is an expression (`EXTRACT(YEAR FROM day) AS yr`) is substituted into the atom, so `yr = 2023` becomes a test on `day`. A MIN, MAX, SUM or AVG is read as its argument (a group of one row). `a = b` between two variables links them to one value. An atom with a window function, a subquery that reads a table or a COUNT is skipped (counts are handled as sizes, below).
3. **Solutions.** Variables that share atoms form a component. Candidate values come from the constants of the atoms and their neighbours, the patterns of `LIKE` and regular expressions, parts of dates (a year for `EXTRACT(YEAR ...)`, a zero-padded month for `CAST(mo AS INT64) = 6`), epoch counts for `TIMESTAMP_MICROS`, and small generic pools. The emulator tests batches of candidate tuples, so any function it evaluates works. A component with several variables is searched one variable at a time (keep the partial assignments that pass the atoms that just became complete); an equality such as `LEFT(CAST(a AS STRING), 5) = b` is met by evaluating its one side and offering the result as the other's value.
4. **Sizes.** `HAVING COUNT(*) > n`, or a derived count compared to a constant, repeats the rows of the anchors it counts (up to 200). `COUNT(DISTINCT x) > n` takes a different `x` in each scenario and the same GROUP BY key. `LIMIT l OFFSET o` and `rank = n` ask for more scenarios (more groups).
5. **Databases.** Each seed writes a few scenarios (coherent sets of rows, one row per anchor, an UNNEST anchor as an element of its parent's array). Odd scenarios leave out the atoms that were read through a derived table (a filter on a year after a window function keeps its earlier-year rows), and each component works from a few of its solutions per database, so scenarios collide on join and group keys.

**What it does not do.** It never changes a label: it only chooses the databases the oracle sees, so a wrong guess leaves a query returning nothing and cannot refute or prove anything. It does not solve window functions, subqueries that read other tables, a comparison between two counts, or values hidden inside an aggregate over an array subquery. It does not know the real tables: the format of a string column comes from how the query parses it. Planning costs a few dozen emulator calls per query.

## Scores

Results: `benchmarks/results/spider2-pairs.json` (summary), `spider2-pairs-dev.json` and `spider2-pairs-held-out.json` (per query); the pairs are in `benchmarks/pairs/spider2-bigquery-dev.jsonl` and `-held-out.jsonl`. emulator v0.8.1, 20 databases per query.

Each cell is the recorded run before this builder, then after. Development: 112 queries; held out: 30. The recorded numbers come from the pair files and the per-query files named above.

| | Development | Held out |
|---|---|---|
| Queries | 112 | 30 |
| Run on the emulator | 80 | 23 |
| Of those, return rows on some database | 49 → 75 | 13 → 22 |
| Pairs | 1,031 | 311 |
| Rewrites (equivalent by construction) | 397 | 127 |
| Rewrites refuted by the oracle | 0 | 0 |
| Rewrites proved by the static layers | 110 | 57 |
| Rewrites on a query that never returns rows | 170 → 27 | 59 → 5 |
| Proved rewrites on a query that returns rows | 74 → 107 | 36 → 56 |
| Mutations | 634 | 184 |
| Mutations refuted, `not_equivalent` with a witness | 123 → 141 | 25 → 27 |
| Mutations whose difference the trust guard withheld | 167 → 265 | 58 → 104 |
| Mutations that agreed on every database | 308 → 201 | 96 → 48 |
| Pairs proved although the emulator refuted them | 0 | 0 |
| Databases (of 20 per query) on which the original returns rows, all queries | 654 → 1,154 | 155 → 338 |

The static layers proved rewrites mostly of the wrapper kinds (CTE rename, CTE wrap) and of operand order; on the development split 107 of the 110 proofs are now on queries that return rows (74 before: the others were proofs of nothing). The proof layers are what this corpus is for: 287 of 397 development rewrites are still unproved, and each is equivalent by construction. The static verdicts were re-scored against the static layers on `main` after the runs: the current layers no longer prove one development pair (`bq430-06`, `COALESCE` written as `IFNULL`; 111 proofs before the re-score), and no other verdict changed.

**The builder was made on the development split only.** The held-out file was regenerated once with the final code. No held-out query was read for this work; the exposure recorded before (`bq001`, `bq004`) is unchanged.

**Still empty (development, 5 of 80):** `bq031` (the emulator crashes on the query with a nil pointer, whatever the data), `bq397` (the schema guess reads CTE column names as table columns), `bq045` (compares two counts of different tables against each other), `ga010` (the channel depends on strings inside an aggregate over an array subquery) and `ga028` (not diagnosed). Held out: one query (`bq302`), not looked at.

## What does not run (development)

32 queries: 8 use geography functions the emulator does not have (`ST_GEOGPOINT`, `ST_AREA`, `ST_DISTANCE`, `ST_INTERSECTS`), 1 is a script with `CREATE`, and 23 fail analysis on the emulator: 10 because a type guess is wrong (a column typed STRING that is summed, a field read on the wrong type, a UNION whose branches disagree), 4 because a column or table is missing from the guess, 4 because the emulator hits a division by zero while analysing the empty tables, 3 because the emulator cannot parse the statement, and 2 because a column name is ambiguous in a join. The reason per query, with the emulator's message, is in `spider2-pairs-dev.json`.

## Limits

- **The data are synthetic and the schemas are guesses.** A column typed `INT64` that runs may be a `STRING` in the real table. Nothing was executed on the public tables.
- **A few runnable queries still return nothing** (5 of 80 development queries, 1 of 23 held out, listed above), and rows are not the same as interesting rows: a query can return one row on built data and still miss the branch a rewrite changes. The builder satisfies filters; it does not aim at the constructs a rewrite touches.
- **Rewrites are equivalent by construction, not proved.** Each is a textbook identity, but nothing here checks that the rule was applied correctly except the oracle and the static layers. A query with a `LIMIT` over tied rows can differ between two equivalent spellings; none did.
- **A mutation's label comes from the emulator.** The trust guard (`node:Between`, `node:In`, `fn:EXTRACT`, `node:Like` are the constructs that withheld most) now withholds 265 development differences (167 before the builder reached more rows), more than the 141 it lets through. A better emulator or a real-BigQuery check on those witnesses is the largest lever left on this corpus. `unlabelled` is not "equivalent".
- **Static proofs run without a schema** (the proof layers take flat types and these schemas are nested), so they work from the queries as written.
- Results are compared as multisets, so a difference only in row order is not seen.
