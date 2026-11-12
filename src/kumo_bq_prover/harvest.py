"""Harvest the constants a pair of queries compares against, to seed test data.

Random values rarely hit the boundary a rewrite changes: the day a date filter
starts on, the string prefix a LIKE looks for, the value next to a `>`. This
module reads the two queries (never their label), finds comparisons between one
column and constants, and returns for each column a list of values worth
trying: the constants and their neighbours.

The result feeds ``datagen.random_database(..., extra=...)``. It only adds
values to the pools, so it can find more counterexamples but cannot make two
different queries look the same.
"""

from __future__ import annotations

import datetime
import decimal

import sqlglot
from sqlglot import exp

from .datagen import normalize_type

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ)


def _const(node: exp.Expression):
    """A constant as ``(kind, value)`` with kind in int, float, str, date, timestamp, or None."""
    if isinstance(node, exp.Paren):
        return _const(node.this)
    if isinstance(node, exp.Neg):
        inner = _const(node.this)
        if inner and inner[0] in ("int", "float"):
            return inner[0], -inner[1]
        return None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "str", node.name
        try:
            number = decimal.Decimal(node.name)
        except decimal.InvalidOperation:
            return None
        return ("int", int(number)) if number == number.to_integral_value() and "." not in node.name and "e" not in node.name.lower() else ("float", float(number))
    if isinstance(node, (exp.Cast, exp.TryCast)) and isinstance(node.this, exp.Literal) and node.this.is_string:
        target = node.to.this
        try:
            if target == exp.DataType.Type.DATE:
                return "date", datetime.date.fromisoformat(node.this.name)
            if target in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.DATETIME):
                return "timestamp", datetime.datetime.fromisoformat(node.this.name.replace("Z", "").split("+")[0])
        except ValueError:
            return None
    return None


def _timestamp(value: datetime.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") + "+00"


def _variants(column_kind: str, const) -> list:
    """Values of the column's type derived from one constant: the constant and its neighbours."""
    kind, value = const
    if column_kind == "INT64":
        if kind in ("int", "float"):
            base = int(value // 1)
            return [base - 1, base, base + 1, base + 2]
    elif column_kind == "FLOAT64":
        if kind in ("int", "float"):
            return [float(value) - 1, float(value), float(value) + 0.5, float(value) + 1]
    elif column_kind in ("NUMERIC", "BIGNUMERIC"):
        if kind in ("int", "float"):
            number = decimal.Decimal(str(value))
            if -number.as_tuple().exponent <= 9:
                return [str(number - 1), str(number), str(number + decimal.Decimal("0.5")), str(number + 1)]
    elif column_kind == "STRING":
        if kind == "str":
            text = value.replace("%", "").replace("_", "")
            return list(dict.fromkeys([value, text, text + "1", text.upper(), text.lower(), text[:-1] if len(text) > 1 else "", " " + text]))
        if kind in ("int", "float"):
            return [str(int(value)) if kind == "int" else str(value)]
    elif column_kind == "DATE":
        day = None
        if kind == "date":
            day = value
        elif kind == "timestamp":
            day = value.date()
        elif kind == "str":
            try:
                day = datetime.date.fromisoformat(value[:10])
            except ValueError:
                return []
        if day:
            one = datetime.timedelta(days=1)
            return [str(day - one), str(day), str(day + one)]
    elif column_kind in ("TIMESTAMP", "DATETIME"):
        moment = None
        if kind == "timestamp":
            moment = value
        elif kind == "date":
            moment = datetime.datetime.combine(value, datetime.time())
        elif kind == "str":
            try:
                moment = datetime.datetime.fromisoformat(value.replace("Z", "").split("+")[0])
            except ValueError:
                return []
        if moment:
            sec, day = datetime.timedelta(seconds=1), datetime.timedelta(days=1)
            points = [moment - day, moment - sec, moment, moment + sec, moment + day]
            if kind == "date":
                points.append(moment + day - sec)  # the last second of that date
            if column_kind == "DATETIME":
                return [p.strftime("%Y-%m-%d %H:%M:%S") for p in points]
            return [_timestamp(p) for p in points]
    return []


def _column_names(node: exp.Expression) -> set[str]:
    return {c.name for c in node.find_all(exp.Column)}


def harvest(left_sql: str, right_sql: str, schema: dict[str, dict[str, str]]) -> dict[str, list]:
    """Per column name, values worth putting in test data. Never raises on unparsable SQL."""
    kinds: dict[str, set[str]] = {}
    for columns in schema.values():
        for name, declared in columns.items():
            kinds.setdefault(name, set()).add(normalize_type(declared))
    found: dict[str, list] = {}

    def add(column: str, const) -> None:
        for kind in kinds.get(column, ()):
            for value in _variants(kind, const):
                bucket = found.setdefault(column, [])
                if value not in bucket:
                    bucket.append(value)

    for sql in (left_sql, right_sql):
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except sqlglot.errors.SqlglotError:
            continue
        for node in tree.walk():
            if isinstance(node, _COMPARISONS):
                sides = [node.this, node.expression]
            elif isinstance(node, exp.Between):
                sides = [node.this, node.args.get("low"), node.args.get("high")]
            elif isinstance(node, exp.In):
                sides = [node.this, *node.expressions]
            elif isinstance(node, (exp.Like, exp.ILike)):
                sides = [node.this, node.expression]
            else:
                continue
            consts = [c for c in (_const(s) for s in sides if s is not None) if c]
            owners: set[str] = set()
            for side in sides:
                if side is not None and _const(side) is None:
                    owners |= _column_names(side)
            if len(owners) == 1:
                (owner,) = owners
                for const in consts:
                    add(owner, const)
    return found
