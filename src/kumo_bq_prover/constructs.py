"""The constructs a query uses, as a set of short tags.

A construct is a function (``fn:SUM``), an operator or clause (``node:Neg``, ``node:Qualify``), a cast
target (``cast:INT64``), a join shape (``join:LEFT:UNNEST``) or a type (``type:NUMERIC``). The compliance
run (``tools/run_compliance.py``) tags each GoogleSQL compliance case the same way, so the failures
it finds can be matched to the queries the prover is asked about.

Tags come from sqlglot's BigQuery parse tree. A query that does not parse gets the single tag ``unparsed``,
which the trust check never trusts.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# Tree nodes that say nothing about behaviour.
_PLAIN = {
    "Column", "Identifier", "Table", "TableAlias", "Alias", "Select", "From", "Where", "Var", "Paren", "Star",
    "Dot", "Boolean", "Null", "Tuple", "Subquery", "Schema", "Properties", "Ordered", "Order", "Limit",
    "Placeholder", "Parameter", "Lateral", "ColumnDef", "Hint", "Cte", "Anonymous", "Literal", "DataType",
    "DataTypeParam", "Expression", "WindowSpec", "Bracket",
}


def is_whole_round(node: exp.Expression) -> bool:
    """``ROUND(x)``, ``ROUND(x, 0)`` or an integer literal: the value is whole, so a following cast to INT64 cannot truncate a fraction."""
    node = node.this if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Literal) and node.is_int:
        return True  # an integer literal is whole too
    if not isinstance(node, exp.Round):
        return False
    digits = node.args.get("decimals")
    return digits is None or (isinstance(digits, exp.Literal) and digits.is_int and int(digits.name) == 0)


def _datatype_name(node: exp.Expression) -> str:
    """The BigQuery spelling of a type's base (``INT64``, ``ARRAY``, ``STRUCT``), without parameters."""
    return node.copy().sql(dialect="bigquery").split("<")[0].split("(")[0].strip().upper() if isinstance(node, exp.DataType) else str(node).upper()


UNPARSED = "unparsed"

#: Format directives of FORMAT_DATE / FORMAT_TIMESTAMP / FORMAT_DATETIME / FORMAT_TIME on which the emulator gave the
#: same text as BigQuery (checked 2026-11-12 on two timestamps, 0 bytes billed; ``docs/emulator-gaps.md``). Not in the
#: set, verified wrong: c e g j U V W w z E*S. Everything else is unverified.
VERIFIED_FORMAT_DIRECTIVES = frozenset(
    {"A", "a", "B", "b", "C", "D", "d", "F", "G", "H", "h", "I", "k", "l", "M", "m", "n", "P", "p", "Q", "R", "S", "s", "T", "t", "u", "X", "x", "Y", "y", "Z", "%", "E4Y", "E3S", "Ez"}
)
_DIRECTIVE = re.compile(r"%([-_0^#]?(?:E\*S|E\d[A-Za-z]|E[A-Za-z]|O[A-Za-z]|[A-Za-z%]))")  # sqlglot rewrites %e as %-d: a flag makes it unverified


def _format_directives(text: str) -> set[str]:
    return set(_DIRECTIVE.findall(text))


def constructs(sql: str | exp.Expression) -> set[str]:
    """The tags of ``sql``; the single tag ``unparsed`` when it does not parse."""
    if isinstance(sql, str):
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except sqlglot.errors.SqlglotError:
            return {UNPARSED}
    else:
        tree = sql
    tags: set[str] = set()
    for node in tree.walk():
        name = type(node).__name__
        if isinstance(node, (exp.Cast, exp.TryCast)):
            # A cast of a string literal (``TIMESTAMP '2020-01-01'`` parses as one) is a different construct
            # from a cast of a column or expression: the emulator parses literals as BigQuery does.
            literal = isinstance(node.this, exp.Literal) and node.this.is_string
            prefix = ("safe_cast" if isinstance(node, exp.TryCast) else "cast") + ("_lit:" if literal else ":")
            target = _datatype_name(node.args["to"])
            if target == "INT64" and is_whole_round(node.this):
                prefix = "cast_rounded:"  # truncating an already rounded value is exact on the emulator (checked on BigQuery)
            tags.add(prefix + target)
        elif isinstance(node, exp.Extract):
            # The part decides whether the emulator is right: WEEK is not (BigQuery numbers weeks from the first
            # Sunday, the emulator does not), the other parts agree with BigQuery (docs/emulator-gaps.md).
            part = node.this
            if isinstance(part, exp.WeekStart):
                tags.add("extract:WEEK")
            else:
                tags.add("extract:" + (part.name if isinstance(part, (exp.Var, exp.Identifier, exp.Literal)) else type(part).__name__).upper())
        elif isinstance(node, (exp.Like, exp.ILike)):
            # LIKE is exact for a literal pattern of ordinary characters and %; `_`, escapes and a pattern that
            # is not a literal (it may hold `_`) are not (docs/emulator-gaps.md).
            pattern = node.args.get("expression")
            plain = isinstance(pattern, exp.Literal) and pattern.is_string and "_" not in pattern.name and "\\" not in pattern.name
            tags.add("like:plain" if plain else "like:wild")
        elif isinstance(node, exp.TimeToStr):
            # FORMAT_DATE, FORMAT_TIMESTAMP and friends: the emulator is exact for the directives checked on BigQuery
            # (VERIFIED_FORMAT_DIRECTIVES) and wrong for others (%j and %e do not pad, %U %W %w number weeks differently).
            fmt = node.args.get("format")
            directives = _format_directives(fmt.name) if isinstance(fmt, exp.Literal) and fmt.is_string else None
            tags.add("format:verified" if directives is not None and directives <= VERIFIED_FORMAT_DIRECTIVES else "format:unverified")
        elif isinstance(node, (exp.DateDiff, exp.DatetimeDiff, exp.TimestampDiff)):
            unit = node.args.get("unit")
            if isinstance(unit, exp.WeekStart):
                if str(unit.this.name if unit.this is not None else "SUNDAY").upper() != "SUNDAY":  # plain WEEK parses as WEEK(SUNDAY): checked
                    tags.add("date_diff:WEEKDAY")
            elif unit is not None and unit.name.upper() == "ISOWEEK":
                tags.add("date_diff:ISOWEEK")
            tags.add("fn:" + node.sql_name().upper())
        elif isinstance(node, exp.Anonymous):
            tags.add("fn:" + str(node.name).upper())
        elif isinstance(node, exp.Func):
            tags.add("fn:" + node.sql_name().upper())
        elif isinstance(node, exp.Join):
            side = (node.args.get("side") or "").upper() or "INNER"
            kind = (node.args.get("kind") or "").upper()
            target = "UNNEST" if isinstance(node.this, exp.Unnest) else "TABLE"
            tags.add(f"join:{side}:{kind + ':' if kind else ''}{target}")
        elif isinstance(node, exp.Is) and isinstance(node.args.get("expression"), exp.Boolean):
            tags.add("is:boolean")
        elif isinstance(node, exp.Literal):
            if node.is_number and any(ch in node.name.lower() for ch in ".e"):
                tags.add("lit:float")
            elif node.is_string:
                tags.add("lit:string")
            else:
                tags.add("lit:int")
        elif isinstance(node, exp.DataType):
            if not isinstance(node.parent, (exp.Cast, exp.TryCast)):  # a cast's target is already its own tag
                tags.add("type:" + _datatype_name(node))
        elif name not in _PLAIN:
            tags.add("node:" + name)
        if isinstance(node, exp.Window):
            spec = node.args.get("spec")
            tags.add("window:frame" if spec is not None else "window:plain")
        if isinstance(node, exp.Select) and node.args.get("distinct"):
            tags.add("node:Distinct")
    return tags
