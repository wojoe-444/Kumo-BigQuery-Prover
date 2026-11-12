#!/usr/bin/env python3
"""Turn a recorded witness into one BigQuery query with the data inlined, to replay it on real BigQuery.

    python tools/witness_to_bigquery.py benchmarks/pairs/spider2-bigquery-dev.jsonl bq077-12

The witness's CREATE TABLE and INSERT statements become common table expressions of typed literal rows, and the
query returns the two result sets of the pair as sorted JSON strings (a multiset comparison, like the oracle's).
Inline constants only: the query reads no table, so it bills 0 bytes. Run it with any BigQuery client and compare
``left_rows`` with ``right_rows``: they should differ, as they did on the emulator.
"""

from __future__ import annotations

import json
import sys

import sqlglot
from sqlglot import exp


def _cte(create: str, inserts: list[str]) -> str | None:
    table = sqlglot.parse_one(create, read="bigquery")
    name = table.this.this.name
    fields = ", ".join(f"{column.name} {column.args['kind'].sql(dialect='bigquery')}" for column in table.this.expressions)
    rows = []
    for statement in inserts:
        insert = sqlglot.parse_one(statement, read="bigquery")
        if insert.this.this.name != name:
            continue
        values = insert.expression
        if not isinstance(values, exp.Values):
            raise ValueError(f"cannot inline: {statement[:80]}")
        for row in values.expressions:  # STRUCT(...) keeps a one-column row from reading as a plain value
            items = row.expressions if isinstance(row, (exp.Tuple, exp.Paren)) and isinstance(row, exp.Tuple) else [row.this if isinstance(row, exp.Paren) else row]
            rows.append("STRUCT(" + ", ".join(item.sql(dialect="bigquery") for item in items) + ")")
    if not rows:
        return f"{name} AS (SELECT * FROM UNNEST(ARRAY<STRUCT<{fields}>>[]))"
    return f"{name} AS (SELECT * FROM UNNEST(ARRAY<STRUCT<{fields}>>[{', '.join(rows)}]))"


def inline_query(setup: list[str], left: str, right: str) -> str:
    creates = [s for s in setup if s.lstrip().upper().startswith("CREATE TABLE")]
    inserts = [s for s in setup if s.lstrip().upper().startswith("INSERT")]
    if len(creates) + len(inserts) != len(setup):
        raise ValueError("the witness has statements other than CREATE TABLE and INSERT")
    ctes = ", ".join(c for c in (_cte(create, inserts) for create in creates) if c)

    def side(sql: str) -> str:
        return f"(SELECT TO_JSON_STRING(ARRAY_AGG(j ORDER BY j)) FROM (SELECT TO_JSON_STRING(x) AS j FROM ({sql.rstrip().rstrip(';')}) AS x))"

    return f"WITH {ctes} SELECT {side(left)} AS left_rows, {side(right)} AS right_rows"


def main() -> int:
    path, wanted = sys.argv[1], sys.argv[2]
    for line in open(path):
        record = json.loads(line)
        if record["id"] == wanted:
            print(inline_query(record["witness"]["setup"], record["left"], record["right"]))
            return 0
    print(f"no pair {wanted} in {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
