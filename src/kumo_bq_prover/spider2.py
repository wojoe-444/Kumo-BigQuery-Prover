"""Turn Spider 2.0 gold queries into labelled BigQuery pairs and score the oracle on them.

Per query (``run_query``):

1. ``schema_infer.prepare`` renames the public tables to ``t_N`` and infers a
   nested schema from how the query uses them. A query that uses a function the
   emulator lacks, or whose schema guess the emulator rejects, is reported as
   "does not run" with the reason; it yields no pairs.
2. ``pairgen.variants`` makes rewrites (equivalent by construction) and mutations.
3. Seeded random databases are built from the schema, seeded with the constants
   the query compares against (``harvest``). Every variant runs on every database
   next to the original.
4. A rewrite whose rows differ from the original on any database is ``WRONG``:
   an emulator bug, a bad rule, or a result that depends on row order under
   LIMIT. A mutation whose rows differ is ``not_equivalent`` and keeps that
   database as its witness; a mutation that never differs stays ``unlabelled``.

The oracle never proves anything here. Every pair also goes through the static proof layers; a proof of a
pair the emulator refuted is reported as ``FALSE_PROOF`` and fails the run.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field

from . import filter_data, nested_datagen, pairgen
from .emulator import Client, EmulatorUnavailable, QueryError
from .harvest import harvest
from .schema_infer import Prepared, Unsupported, prepare, type_sql
from .static import prove_static_detailed
from .trust import adjudicate
from .values import same_rows
from .verdict import Verdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPIDER2 = ROOT / "benchmarks" / "spider2"

#: Rows kept in a stored witness, per side.
WITNESS_ROWS = 200


def split_of(case_id: str) -> str:
    """Every fifth task (by SHA-1 of its id) is held out; the same split as KumoSQL's Spider 2.0 eval."""
    return "held-out" if int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 5 == 0 else "dev"


def manifest() -> list[dict]:
    return json.loads((SPIDER2 / "manifest.json").read_text())["cases"]


def gold_sql(case_id: str) -> str:
    return (SPIDER2 / "gold" / f"{case_id}.sql").read_text(encoding="utf-8")


def schema_json(schema: dict[str, dict]) -> dict[str, dict[str, str]]:
    return {table: {column: type_sql(kind) for column, kind in columns.items()} for table, columns in schema.items()}


def _try_run(client: Client, dataset: str, sql: str):
    try:
        return client.query(sql, dataset), None
    except QueryError as error:
        return None, str(error)


def _clean_error(message: str) -> str:
    import re

    found = re.search(r'"message":"(?:failed to \w+: )?([^"]{0,200})', message.replace('\\"', '"'))
    text = found.group(1) if found else message[:200]
    return re.sub(r"\s+", " ", text)[:160]


def choose_schema(client: Client, prepared: Prepared) -> tuple[dict | None, str]:
    """The first schema variant on which the emulator analyses the query (on empty tables)."""
    reason = ""
    for choice in prepared.schema.variants():
        schema = prepared.schema.local_schema(choice)
        try:
            with client.scratch_dataset() as dataset:
                for statement in nested_datagen.create_statements(schema):
                    client.query(statement, dataset)
                client.query(prepared.sql, dataset)
            return schema, ""
        except QueryError as error:
            reason = _clean_error(str(error))
    return None, reason


@dataclass
class QueryRun:
    case_id: str
    status: str  # "ok", "unsupported", "analysis-error", "no-data-run"
    reason: str = ""
    sql: str = ""
    schema: dict = field(default_factory=dict)
    databases: int = 0
    databases_nonempty: int = 0  # databases on which the original returned at least one row
    databases_failed: int = 0  # databases on which the original itself raised an error
    base_error: str = ""
    builder: dict = field(default_factory=dict)  # what the filter-satisfying data builder found (see filter_data)
    databases_nonempty_filtered: int = 0  # of databases_nonempty, those built by the filter-satisfying builder
    pairs: list[dict] = field(default_factory=list)


def run_query(
    client: Client,
    case_id: str,
    sql: str | None = None,
    databases: int = 20,
    max_mutations: int = 8,
    filtered_share: float = 0.75,
) -> QueryRun:
    """``filtered_share`` of the databases come from ``filter_data`` (rows that satisfy the query's own filters,
    joins and UNNESTs); the rest are the plain random databases, which keep their variety."""
    sql = sql if sql is not None else gold_sql(case_id)
    try:
        prepared = prepare(sql)
    except Unsupported as error:
        return QueryRun(case_id, "unsupported", str(error))
    except Exception as error:  # noqa: BLE001 - report, never crash a whole run on one query
        return QueryRun(case_id, "unsupported", f"internal: {type(error).__name__}: {str(error)[:100]}")
    schema, reason = choose_schema(client, prepared)
    if schema is None:
        return QueryRun(case_id, "analysis-error", reason, sql=prepared.sql)
    flat = nested_datagen.flatten(schema)
    extra = harvest(prepared.sql, prepared.sql, flat)
    exact = harvest(prepared.sql, prepared.sql, flat, exact=True)
    classes = prepared.schema.classes()
    flavours = prepared.schema.flavours()
    variants = pairgen.variants(prepared.tree, max_mutations=max_mutations)
    run = QueryRun(case_id, "ok", sql=prepared.sql, schema=schema_json(schema), databases=databases)
    plan = None
    if filtered_share > 0:
        try:
            plan = filter_data.plan(client, prepared.tree, schema, flavours=flavours)
        except EmulatorUnavailable as error:
            run.status, run.reason = "no-data-run", f"emulator unavailable: {error}"
            return run
        except Exception as error:  # noqa: BLE001 - the builder is an aid: without it the random databases still run
            run.builder = {"error": f"{type(error).__name__}: {str(error)[:100]}"}
        if plan is not None:
            run.builder = plan.summary()
            if not plan.solvable():
                plan = None
    stats = [{"compared": 0, "differ": 0, "errors": 0, "witness": None, "untrusted": {}} for _ in variants]
    for seed in range(databases):
        filtered = plan is not None and int((seed + 1) * filtered_share + 1e-9) > int(seed * filtered_share + 1e-9)
        if filtered:
            setup = plan.database(seed, extra=extra, classes=classes, exact=exact, flavours=flavours)
        else:
            setup = nested_datagen.random_database(schema, seed, extra=extra, classes=classes, exact=exact, flavours=flavours)
        try:
            with client.scratch_dataset() as dataset:
                for statement in setup:
                    client.query(statement, dataset)
                base, error = _try_run(client, dataset, prepared.sql)
                if base is None:
                    run.databases_failed += 1
                    run.base_error = run.base_error or _clean_error(error or "")
                    continue
                if base.rows:
                    run.databases_nonempty += 1
                    run.databases_nonempty_filtered += bool(filtered)
                for stat, variant in zip(stats, variants):
                    other, error = _try_run(client, dataset, variant.sql)
                    if other is None:
                        stat["errors"] += 1
                        stat.setdefault("error", _clean_error(error or ""))
                        continue
                    stat["compared"] += 1
                    if not same_rows(base.rows, other.rows):
                        # The emulator is not BigQuery: a difference involving a construct or value the
                        # compliance run showed it getting wrong is not a counterexample (see trust.py).
                        verdict = adjudicate(
                            lambda sql: client.query(sql, dataset).rows, prepared.sql, variant.sql, setup, base.rows, other.rows
                        )
                        if not verdict.trusted:
                            for reason in verdict.reasons:
                                stat["untrusted"][reason] = stat["untrusted"].get(reason, 0) + 1
                            continue
                        stat["differ"] += 1
                        stat["rewritten"] = stat.get("rewritten", 0) + bool(verdict.rewritten)
                        if stat["witness"] is None:
                            stat["witness"] = {
                                "setup": setup,
                                "left_rows": [list(map(_plain, r)) for r in verdict.left_rows[:WITNESS_ROWS]],
                                "right_rows": [list(map(_plain, r)) for r in verdict.right_rows[:WITNESS_ROWS]],
                                "left_row_count": len(verdict.left_rows),
                                "right_row_count": len(verdict.right_rows),
                            }
                            if verdict.rewritten:
                                stat["witness"]["checked_with_rewritten_queries"] = True
        except EmulatorUnavailable as error:
            run.status, run.reason = "no-data-run", f"emulator unavailable: {error}"
            return run
        except QueryError as error:
            run.reason = f"setup failed: {_clean_error(str(error))}"
            continue
    for index, (variant, stat) in enumerate(zip(variants, stats), start=1):
        distrusted = bool(stat["untrusted"]) and not stat["differ"]
        if variant.kind == "equivalent":
            outcome = "WRONG" if stat["differ"] else ("untrusted" if distrusted else ("agreed" if stat["compared"] else "not-run"))
            label = "equivalent"
        else:
            outcome = "refuted" if stat["differ"] else ("untrusted" if distrusted else ("not-run" if not stat["compared"] else "agreed"))
            label = "not_equivalent" if stat["differ"] else "unlabelled"
        record = {
            "id": f"{case_id}-{index:02d}",
            "query": case_id,
            "kind": "rewrite" if variant.kind == "equivalent" else "mutation",
            "rule": variant.rule,
            "label": label,
            "oracle": outcome,
            "left": prepared.sql,
            "right": variant.sql,
            "schema": run.schema,
            "databases_compared": stat["compared"],
            "databases_differ": stat["differ"],
            "databases_error": stat["errors"],
            "left_returns_rows": run.databases_nonempty > 0,
        }
        static = prove_static_detailed(prepared.sql, variant.sql)
        record["static"] = {"verdict": static.verdict.value, "layer": static.layer}
        if static.withheld:
            record["static"]["withheld"] = static.withheld
        record["static_proof"] = static.verdict is Verdict.PROVEN
        if record["static_proof"] and label == "not_equivalent":
            record["oracle"] = "FALSE_PROOF"  # a proof of a pair the emulator refuted: a bug in a proof layer or in the emulator
        if stat.get("rewritten"):
            record["databases_differ_after_rewrite"] = stat["rewritten"]  # differences checked with IN/BETWEEN/IS TRUE written as plain operators
        if stat["untrusted"]:
            record["untrusted"] = stat["untrusted"]  # differences left unreported because the emulator is not trusted there
        if stat.get("error") and not stat["compared"]:
            record["error"] = stat["error"]
        if stat["witness"] and variant.kind == "mutation":
            record["witness"] = stat["witness"]
        elif stat["witness"]:
            record["counterexample"] = stat["witness"]  # a rewrite refuted: look at it by hand
        run.pairs.append(record)
    return run


def _plain(value):
    """JSON-safe form of a decoded cell."""
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
