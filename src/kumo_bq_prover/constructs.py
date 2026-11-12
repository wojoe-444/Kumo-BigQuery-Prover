"""The constructs a query uses, as a set of short tags.

A construct is a function (``fn:SUM``), an operator or clause (``node:Neg``, ``node:Qualify``), a cast
target (``cast:INT64``), a join shape (``join:LEFT:UNNEST``) or a type (``type:NUMERIC``). The compliance
run (``tools/run_compliance.py``) tags each GoogleSQL compliance case the same way, so the failures
it finds can be matched to the queries the prover is asked about.

Tags come from sqlglot's BigQuery parse tree. A query that does not parse gets the single tag ``unparsed``,
which the trust check never trusts.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

# Tree nodes that say nothing about behaviour.
_PLAIN = {
    "Column", "Identifier", "Table", "TableAlias", "Alias", "Select", "From", "Where", "Var", "Paren", "Star",
    "Dot", "Boolean", "Null", "Tuple", "Subquery", "Schema", "Properties", "Ordered", "Order", "Limit",
    "Placeholder", "Parameter", "Lateral", "ColumnDef", "Hint", "Cte", "Anonymous", "Literal", "DataType",
    "DataTypeParam", "Expression", "WindowSpec", "Bracket",
}


def _datatype_name(node: exp.Expression) -> str:
    """The BigQuery spelling of a type's base (``INT64``, ``ARRAY``, ``STRUCT``), without parameters."""
    return node.copy().sql(dialect="bigquery").split("<")[0].split("(")[0].strip().upper() if isinstance(node, exp.DataType) else str(node).upper()


UNPARSED = "unparsed"


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
            tags.add(prefix + _datatype_name(node.args["to"]))
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
