# Jaffle Shop refactors on the emulator

**Purpose:** check whether the emulator oracle tells a harmless refactor of a multi-model pipeline from a breaking one. The pipeline is the dbt project [jaffle_shop_duckdb](https://github.com/dbt-labs/jaffle_shop_duckdb) (Apache-2.0, pinned commit in `benchmarks/pairs/jaffle-shop/README.md`): three seed tables and five models (`stg_customers`, `stg_orders`, `stg_payments`, `customers`, `orders`). The 15 refactors (8 equivalent, 7 not) were authored for KumoSQL's earlier eval; this repository scores them.

**Dialect:** the models are standard SQL that BigQuery accepts as written (CTEs, `LEFT JOIN`, `GROUP BY`, `CASE`, `SUM`, `/`), so no translation is involved. The Jinja is rendered once; `ref('x')` is kept as `@@x@@` and becomes a dataset-qualified name when the models are built, as dbt does. Seed types are BigQuery's (`INT64`, `STRING`, `DATE`). Unlike the two single-query corpora this is a multi-statement pipeline, so it has its own runner.

**Example:** `customers_inner_join_orders` turns the final `LEFT JOIN customer_orders` into an inner join: customers with no orders disappear from `customers`. The upstream seed data already shows it.

**Files:** `benchmarks/pairs/jaffle-shop-refactors.jsonl` (`before` and `after` models in dependency order), `benchmarks/pairs/jaffle-shop/seeds.json` (the seed rows, and an authored `edge` database), `LICENSE`. Result: `benchmarks/results/pairs-jaffle-shop-refactors.json`.

**Run:** `python tools/run_jaffle_refactors.py --databases 8`. For each of 10 databases (the seeds, the edge database and 8 random ones) both pipelines are built with `CREATE TABLE name AS <model>` and every model present in both is compared as a multiset, FLOAT64 rounded to 6 digits. A fresh emulator starts per database.

## Scores (emulator v0.8.1, refutation only)

| | Count |
|---|---|
| Equivalent refactors (8) left alone | 8, none refuted |
| Different refactors (7) refuted | 7 |

Three of the seven already differ on the upstream seeds; the other four (`inner_join_order_payments`, `pivot_coalesce`, `count_star_orders`, `count_distinct_orders`) differ only on the edge database, which carries the NULL corners the seeds lack. 0 wrong. Nothing is proved.

## Limits

15 cases on one project, no held-out split here. The first run left one equivalent refactor unchecked because `ref('orders')` was rendered as a bare name that a CTE called `orders` shadowed; qualifying the name fixed it, and the final run is the one above. The runner builds only what these cases need: five models and three seed tables.
