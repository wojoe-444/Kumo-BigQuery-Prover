# Jaffle Shop refactors

`../jaffle-shop-refactors.jsonl` holds 15 refactors (8 equivalent, 7 not) of the dbt project [jaffle_shop_duckdb](https://github.com/dbt-labs/jaffle_shop_duckdb) (Apache-2.0, `LICENSE` here), pinned at commit `20cc904370818ceab08cf19f8fd196e9e81def88`. The refactors are authored (by KumoSQL's earlier eval), not upstream; the five models are upstream's with the Jinja rendered so that `ref('x')` becomes the table name `x`. `seeds.json` holds the three seed CSVs as typed rows (`seeds`, from upstream's `seeds/`) and an authored `edge` database of NULL corners.

**Dialect:** the models are standard SQL that BigQuery accepts as written (CTEs, `LEFT JOIN`, `GROUP BY`, `CASE`, `SUM`, `/`), so no translation is involved. Seed column types are the BigQuery types of the dbt-inferred ones (`INT64`, `STRING`, `DATE`).
