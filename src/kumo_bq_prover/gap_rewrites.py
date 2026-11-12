"""Rewrite a query so it avoids the emulator's three-valued-logic gaps, keeping BigQuery's answer.

The emulator gets a few NULL cases wrong where BigQuery is exact (``docs/emulator-gaps.md``): ``3 IN (1, 2, NULL)``
is FALSE there and NULL on BigQuery, ``10 BETWEEN NULL AND 5`` is NULL there and FALSE on BigQuery, and
``NULL IS TRUE`` is NULL there and FALSE on BigQuery. Plain ``AND``, ``OR``, ``NOT``, ``=``, ``COALESCE``, ``CASE``
and ``EXISTS`` are right on NULL (checked against BigQuery, ``tests/test_gap_rewrites.py``). Each rewrite below
writes the construct with those operators and nothing else, so the rewritten query has the same answer on BigQuery
and does not depend on the gap:

* ``x IN (a, b)``            -> ``x = a OR x = b`` (three-valued ``OR`` is the definition of ``IN``)
* ``x BETWEEN a AND b``      -> ``x >= a AND x <= b`` (the definition of ``BETWEEN``)
* ``x IS TRUE``              -> ``COALESCE(x, FALSE)``; ``IS FALSE`` -> ``COALESCE(NOT x, FALSE)`` (``NOT`` the other way round)
* ``CAST(x AS INT64)``       -> ``CAST(ROUND(x) AS INT64)`` (BigQuery rounds half away from zero where the emulator truncates)
* ``x IN (SELECT v ...)``    -> ``CASE WHEN EXISTS (v = x) THEN TRUE WHEN EXISTS (v IS NULL OR x IS NULL) THEN NULL ELSE FALSE END``
* ``x IN UNNEST(arr)``       -> the same ``CASE`` over the elements of ``arr``

``x IN (SELECT ...)`` is NULL on BigQuery when no row equals ``x`` and either some row is NULL or ``x`` is NULL and
the subquery has a row; FALSE for an empty subquery. ``IN UNNEST`` of a NULL array is FALSE like an empty array.

The rewrite is used only to *check a refutation* (``trust.adjudicate``): the oracle runs the rewritten pair on the same
data, and a difference that survives is a counterexample for BigQuery whatever the original ``IN`` did on the emulator.
It is not a proof tool and never changes a query the user sees. When a construct cannot be rewritten exactly
(a row-valued ``IN``, a set-operation subquery, a ``SELECT *`` subquery, an aggregate or window function as the left
operand of ``IN (SELECT ...)``, a volatile function) the function returns ``None`` and the refutation stays UNKNOWN.
"""

from __future__ import annotations

import itertools

import sqlglot
from sqlglot import exp

from .constructs import constructs, is_whole_round

#: The construct tags (``constructs.py``) these rewrites remove.
REWRITABLE = frozenset({"node:In", "node:Between", "is:boolean", "cast:INT64", "safe_cast:INT64"})

MAX_ROUNDS = 8
_VOLATILE_NAMES = {"RAND", "GENERATE_UUID", "CURRENT_DATE", "CURRENT_DATETIME", "CURRENT_TIME", "CURRENT_TIMESTAMP", "SESSION_USER", "NOW", "UUID"}


class _Unsupported(Exception):
    """This construct cannot be rewritten exactly."""


def _volatile(node: exp.Expression) -> bool:
    for sub in node.walk():
        if isinstance(sub, (exp.Rand, exp.Uuid, exp.CurrentDate, exp.CurrentDatetime, exp.CurrentTime, exp.CurrentTimestamp)):
            return True
        if isinstance(sub, exp.Anonymous) and str(sub.name).upper() in _VOLATILE_NAMES:
            return True
        if isinstance(sub, exp.Func) and sub.sql_name().upper() in _VOLATILE_NAMES:
            return True
    return False


def _has_aggregate_or_window(node: exp.Expression) -> bool:
    return any(isinstance(sub, (exp.AggFunc, exp.Window)) for sub in node.walk())


def _bool(node: exp.Expression) -> exp.Expression:
    return exp.Paren(this=node)


def _in_rewrite(node: exp.In, names: "itertools.count") -> exp.Expression:
    left = node.this
    if isinstance(left, (exp.Tuple, exp.Struct)) or _volatile(node):
        raise _Unsupported
    values = node.args.get("expressions")
    query = node.args.get("query")
    unnest = node.args.get("unnest")
    if values:
        if any(isinstance(value, (exp.Tuple, exp.Struct)) for value in values):
            raise _Unsupported
        terms = [exp.EQ(this=left.copy(), expression=value.copy()) for value in values]
        combined = terms[0]
        for term in terms[1:]:
            combined = exp.Or(this=combined, expression=term)
        return _bool(combined)
    index = next(names)
    alias = f"_gap{index}"
    if unnest is not None:
        if not isinstance(unnest, exp.Unnest) or unnest.args.get("offset"):
            raise _Unsupported

        def over(predicate: exp.Expression) -> exp.Expression:
            source = exp.Unnest(expressions=[e.copy() for e in unnest.expressions], alias=exp.TableAlias(columns=[exp.to_identifier(alias)]))
            # UNNEST(arr) AS alias: the element is addressed by the alias itself in BigQuery.
            select = exp.select("1").from_(source).where(predicate)
            return exp.Exists(this=select)

        element = exp.column(alias)
        eq = over(exp.EQ(this=element.copy(), expression=left.copy()))
        gap = over(exp.Or(this=exp.Is(this=element.copy(), expression=exp.Null()), expression=exp.Is(this=left.copy(), expression=exp.Null())))
    elif isinstance(query, exp.Subquery) and isinstance(query.this, exp.Select):
        if _has_aggregate_or_window(left):
            raise _Unsupported
        inner = query.this
        if len(inner.expressions) != 1 or isinstance(inner.expressions[0].unalias(), exp.Star):
            raise _Unsupported
        projected = inner.copy()
        projected.set("expressions", [exp.alias_(projected.expressions[0].unalias(), f"_gapv{index}", quoted=False)])

        def over(predicate: exp.Expression) -> exp.Expression:
            derived = exp.Subquery(this=projected.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))
            return exp.Exists(this=exp.select("1").from_(derived).where(predicate))

        element = exp.column(f"_gapv{index}", table=alias)
        eq = over(exp.EQ(this=element.copy(), expression=left.copy()))
        gap = over(exp.Or(this=exp.Is(this=element.copy(), expression=exp.Null()), expression=exp.Is(this=left.copy(), expression=exp.Null())))
    else:
        raise _Unsupported
    case = exp.Case(ifs=[exp.If(this=eq, true=exp.true()), exp.If(this=gap, true=exp.null())], default=exp.false())
    return _bool(case)


def _between_rewrite(node: exp.Between) -> exp.Expression:
    if _volatile(node):
        raise _Unsupported
    this, low, high = node.this, node.args["low"], node.args["high"]
    return _bool(exp.And(this=exp.GTE(this=this.copy(), expression=low.copy()), expression=exp.LTE(this=this.copy(), expression=high.copy())))


def _is_boolean_rewrite(node: exp.Is) -> exp.Expression:
    value = node.args["expression"]
    if _volatile(node):
        raise _Unsupported
    this = node.this.copy()
    if value.this:  # IS TRUE
        return exp.Coalesce(this=this, expressions=[exp.false()])
    return exp.Coalesce(this=exp.Not(this=_bool(this)), expressions=[exp.false()])


def _cast_rewrite(node: exp.Cast | exp.TryCast) -> exp.Expression:
    """``CAST(x AS INT64)`` -> ``CAST(ROUND(x) AS INT64)``: BigQuery rounds half away from zero, the emulator truncates.

    ``ROUND`` is exact on the emulator for FLOAT64 and NUMERIC (checked on BigQuery); for a STRING or BOOL operand it has
    no signature, so the emulator rejects the query and the difference stays withheld. INT64 operands pass through
    ``ROUND`` as FLOAT64, so the caller refuses integers from 2^53 (see ``trust.adjudicate``).
    """
    to = node.args.get("to")
    if not isinstance(to, exp.DataType) or to.sql(dialect="bigquery").upper() != "INT64":
        return node
    operand = node.this
    if is_whole_round(operand):
        return node
    if isinstance(operand, exp.Literal) and operand.is_string:
        raise _Unsupported  # CAST('2.5' AS INT64): string parsing, not a float conversion
    if _volatile(node):
        raise _Unsupported
    return node.__class__(this=exp.Round(this=operand.copy()), to=to.copy())


def rewrite(sql: str) -> str | None:
    """``sql`` with every ``IN``, ``BETWEEN`` and ``IS [NOT] TRUE|FALSE`` written with plain operators.

    Returns ``None`` when the query does not parse or some occurrence cannot be rewritten exactly; returns the
    (re-rendered) query unchanged in meaning otherwise.
    """
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
    names = itertools.count(1)

    def convert(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.In):
            return _in_rewrite(node, names)
        if isinstance(node, exp.Between):
            return _between_rewrite(node)
        if isinstance(node, exp.Is) and isinstance(node.args.get("expression"), exp.Boolean):
            return _is_boolean_rewrite(node)
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return _cast_rewrite(node)
        return node

    result = tree
    try:
        for _ in range(MAX_ROUNDS):  # a rewritten node is not walked again, so nested INs need another round
            result = result.transform(convert, copy=True)
            if not constructs(result) & REWRITABLE:
                break
        else:
            return None
    except _Unsupported:
        return None
    try:
        return result.sql(dialect="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
