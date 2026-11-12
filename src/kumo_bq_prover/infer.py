"""Guess a schema for queries that come without one, so the emulator can build data for them.

The guess is only for generating test data. The static layers never see it: a proof needs a declared schema, and a
pair without one is proved (or not) with no column types. A wrong guess makes the emulator reject a query, which the
callers treat as "could not check", never as an agreement.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .datagen import normalize_type

_STRING_FUNCTIONS = (exp.Lower, exp.Upper, exp.Like, exp.ILike, exp.Concat, exp.DPipe, exp.Substring, exp.Trim, exp.Length, exp.RegexpLike)
_DATE_LITERAL = (exp.Date, exp.Cast)


def _kind_of(literal: exp.Expression) -> str | None:
    if isinstance(literal, exp.Literal):
        if literal.is_string:
            return "STRING"
        return "INT64" if literal.name.lstrip("-").isdigit() else "FLOAT64"
    if isinstance(literal, exp.Boolean):
        return "BOOL"
    return None


def infer_schema(queries: list[str]) -> dict[str, dict[str, str]]:
    """``{table: {column: type}}`` for the base tables the queries read, or ``{}`` when they do not parse."""

    trees = []
    for sql in queries:
        try:
            trees.append(sqlglot.parse_one(sql, read="bigquery"))
        except sqlglot.errors.SqlglotError:
            return {}
    derived = {a.alias.lower() for tree in trees for a in tree.find_all(exp.CTE)} | {
        s.alias.lower() for tree in trees for s in tree.find_all(exp.Subquery) if s.alias
    }
    defined = {a.alias.lower() for tree in trees for a in tree.find_all(exp.Alias) if a.alias}
    defined |= {name.lower() for tree in trees for u in tree.find_all(exp.Unnest) for name in _unnest_names(u)}
    tables: dict[str, dict[str, str]] = {}
    for tree in trees:
        aliases: dict[str, str] = {}
        sources: list[str] = []
        for table in tree.find_all(exp.Table):
            name = table.name.lower()
            if not name or name in derived or isinstance(table.parent, exp.Unnest):
                continue
            tables.setdefault(name, {})
            sources.append(name)
            aliases[(table.alias or name).lower()] = name
        for column in tree.find_all(exp.Column):
            name = column.name.lower()
            if not name or name in defined and not column.table:
                continue
            owners = [aliases[column.table.lower()]] if column.table and column.table.lower() in aliases else (sources if not column.table else [])
            guess = _guess_type(column)
            for owner in owners:
                tables[owner][name] = _stronger(tables[owner].get(name), guess)
    return {table: {col: normalize_type(kind) for col, kind in columns.items()} for table, columns in tables.items() if columns}


def _unnest_names(unnest: exp.Unnest) -> list[str]:
    alias = unnest.args.get("alias")
    if alias is None:
        return []
    return [alias.name] + [column.name for column in alias.args.get("columns") or []]


def _guess_type(column: exp.Column) -> str:
    parent = column.parent
    if isinstance(parent, _STRING_FUNCTIONS) or isinstance(parent, exp.Like):
        return "STRING"
    if isinstance(parent, (exp.Sum, exp.Avg)):
        return "INT64"
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)):
        for sibling in parent.args.values():
            for node in sibling if isinstance(sibling, list) else [sibling]:
                if node is not column and isinstance(node, exp.Expression):
                    kind = _kind_of(node)
                    if kind:
                        return kind
    return "INT64"


def _stronger(old: str | None, new: str) -> str:
    # A column read both as a number by default and as a string by a literal is a string: the literal is evidence.
    if old is None or old == "INT64":
        return new
    return old
