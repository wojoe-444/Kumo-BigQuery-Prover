"""Infer table schemas from the queries that read them.

The Spider 2.0 gold queries read public BigQuery tables we cannot reach, so the
only evidence about a table's columns is how a query uses them. This module
parses one query, finds every physical table and every column path read from
it (``t.a``, ``t.a.b``, ``UNNEST(t.arr) AS x`` then ``x.f``), collects what the
query does with each (compare to a string, add to a number, EXTRACT a year) and
settles on a type per column, including ARRAY and STRUCT.

Nothing here has to be right for the results to be sound. A wrong guess makes
the emulator reject the query, which the caller reports as "does not run"; it
cannot produce a wrong label. The guess only decides how many queries run.

Two ambiguous guesses carry alternatives so a caller can retry:
``Schema.variants()`` yields the schema with date-like columns flipped between
DATE, TIMESTAMP and DATETIME, and number-like columns between INT64 and FLOAT64.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope, walk_in_scope

# A column type: "INT64", ("ARRAY", elem), or ("STRUCT", ((name, type), ...)).
ColType = str | tuple

_TYPE_NAMES = {
    exp.DataType.Type.DATE: "DATE",
    exp.DataType.Type.TIMESTAMP: "TIMESTAMP",
    exp.DataType.Type.TIMESTAMPTZ: "TIMESTAMP",
    exp.DataType.Type.DATETIME: "DATETIME",
    exp.DataType.Type.INT: "INT64",
    exp.DataType.Type.BIGINT: "INT64",
    exp.DataType.Type.SMALLINT: "INT64",
    exp.DataType.Type.TINYINT: "INT64",
    exp.DataType.Type.DOUBLE: "FLOAT64",
    exp.DataType.Type.FLOAT: "FLOAT64",
    exp.DataType.Type.DECIMAL: "NUMERIC",
    exp.DataType.Type.BIGDECIMAL: "NUMERIC",
    exp.DataType.Type.VARCHAR: "STRING",
    exp.DataType.Type.TEXT: "STRING",
    exp.DataType.Type.BOOLEAN: "BOOL",
}

#: Votes in priority order; the first present in a column's votes decides its type.
_PRIORITY = ["DATE", "TIMESTAMP", "DATETIME", "BOOL", "STRING", "DATEISH", "FLOAT64", "INT64", "NUM", "STRING_LIT", "WEAK_STRING"]
_SCALAR_OF = {"NUM": "INT64", "DATEISH": "DATE", "WEAK_STRING": "STRING", "STRING_LIT": "STRING"}


class Unsupported(Exception):
    """The query cannot be turned into a runnable case; the message says why."""


class Node:
    """One column, struct field or array element, with union-find so equal columns share a type."""

    def __init__(self) -> None:
        self.parent: Node | None = None
        self.votes: set[str] = set()
        self.fields: dict[str, tuple[str, Node]] = {}  # lower-case name -> (display name, node)
        self.elem: Node | None = None

    def find(self) -> "Node":
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def child(self, name: str) -> "Node":
        root = self.find()
        if root.elem is not None:  # a field of an array's element
            return root.elem.child(name)
        key = name.lower()
        if key not in root.fields:
            root.fields[key] = (name, Node())
        return root.fields[key][1]

    def element(self) -> "Node":
        root = self.find()
        if root.elem is None:
            root.elem = Node()
            for key, value in root.fields.items():  # fields seen before the array was known
                root.elem.find().fields[key] = value
            root.fields = {}
        return root.elem


def link(a: Node, b: Node) -> None:
    a, b = a.find(), b.find()
    if a is b:
        return
    b.parent = a
    a.votes |= b.votes
    for key, (name, node) in b.fields.items():
        if key in a.fields:
            link(a.fields[key][1], node)
        else:
            a.fields[key] = (name, node)
    if b.elem is not None:
        if a.elem is None:
            a.elem = b.elem
        else:
            link(a.elem, b.elem)


@dataclass
class Table:
    key: str
    local: str  # the name in the rewritten query, t_0, t_1, ...
    columns: dict[str, tuple[str, Node]] = field(default_factory=dict)
    wildcard: bool = False

    def column(self, name: str) -> Node:
        key = name.lower()
        if key not in self.columns:
            self.columns[key] = (name, Node())
        return self.columns[key][1]


@dataclass
class Schema:
    tables: dict[str, Table]  # by key
    ambiguous: list[tuple[Node, list[str]]] = field(default_factory=list)

    def local_schema(self, choice: dict[int, str] | None = None) -> dict[str, dict[str, ColType]]:
        """``{local table: {column: type}}``; ``choice`` overrides ambiguous columns by index."""
        out: dict[str, dict[str, ColType]] = {}
        overrides = {}
        for index, (node, _) in enumerate(self.ambiguous):
            if choice and index in choice:
                overrides[id(node.find())] = choice[index]
        for table in self.tables.values():
            out[table.local] = {name: _final(node, overrides) for name, node in table.columns.values()}
        return out

    def classes(self) -> dict[tuple, int]:
        """``(table, column, field, ...)`` -> id of the equality class the column belongs to."""
        out: dict[tuple, int] = {}

        def walk(key: tuple, node: Node) -> None:
            root = node.find()
            out[key] = id(root)
            if root.elem is not None:
                root = root.elem.find()
                out[key] = id(root)
            for name, child in root.fields.values():
                walk(key + (name,), child)

        for table in self.tables.values():
            for name, node in table.columns.values():
                walk((table.local, name), node)
        return out

    def flavours(self) -> dict[tuple, str]:
        """``(table, column, field, ...)`` -> the format a STRING column must have (see ``_CAST_FORMAT``)."""
        out: dict[tuple, str] = {}

        def walk(key: tuple, node: Node) -> None:
            root = node.find()
            if root.elem is not None:
                root = root.elem.find()
            tags = sorted(v[4:] for v in root.votes if v.startswith("FMT:"))
            if tags and not root.fields:
                out[key] = tags[0]
            for name, child in root.fields.values():
                walk(key + (name,), child)

        for table in self.tables.values():
            for name, node in table.columns.values():
                walk((table.local, name), node)
        return out

    def variants(self, limit: int = 8):
        """Yield override dicts: the default first, then flips of the ambiguous columns."""
        yield {}
        options = [alts for _, alts in self.ambiguous]
        if not options:
            return
        count = 0
        for combo in itertools.product(*[range(len(alts)) for alts in options]):
            if not any(combo):
                continue
            yield {i: options[i][c] for i, c in enumerate(combo)}
            count += 1
            if count >= limit - 1:
                return


def _final(node: Node, overrides: dict[int, str]) -> ColType:
    root = node.find()
    if root.elem is not None:
        elem = root.elem.find()
        if elem.fields:
            return ("ARRAY", ("STRUCT", tuple((name, _final(n, overrides)) for name, n in elem.fields.values())))
        return ("ARRAY", _final(elem, overrides))
    if root.fields:
        return ("STRUCT", tuple((name, _final(n, overrides)) for name, n in root.fields.values()))
    if id(root) in overrides:
        return overrides[id(root)]
    for vote in _PRIORITY:
        if vote in root.votes:
            return _SCALAR_OF.get(vote, vote)
    return "STRING"


#: What a STRING column must look like to survive CAST/PARSE_DATE on it: a strftime format,
#: or the pseudo formats ``int`` and ``float`` for numbers.
_CAST_FORMAT = {
    "INT64": "int", "FLOAT64": "float", "NUMERIC": "float",
    "DATE": "%Y-%m-%d", "TIMESTAMP": "%Y-%m-%d %H:%M:%S", "DATETIME": "%Y-%m-%d %H:%M:%S",
}


def _python_format(fmt: str) -> str:
    """GoogleSQL format elements that differ from Python's strftime: %F and %T."""
    return fmt.replace("%F", "%Y-%m-%d").replace("%T", "%H:%M:%S")


def type_sql(kind: ColType) -> str:
    """GoogleSQL type text for a ColType."""
    if isinstance(kind, str):
        return kind
    if kind[0] == "ARRAY":
        return f"ARRAY<{type_sql(kind[1])}>"
    return "STRUCT<" + ", ".join(f"{name} {type_sql(t)}" for name, t in kind[1]) + ">"


_SHARD = re.compile(r"[_]?(\d{6,8}|\*)$")


def table_key(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    parts[-1] = _SHARD.sub("", parts[-1])
    return ".".join(parts).lower()


_STRING_ARG = (
    exp.Lower, exp.Upper, exp.Trim, exp.Concat, exp.RegexpLike, exp.RegexpILike, exp.Split, exp.Substring,
    exp.StartsWith, exp.EndsWith, exp.Left, exp.Right, exp.Initcap, exp.Length, exp.Replace, exp.Like, exp.ILike,
    exp.GroupConcat, exp.RegexpExtract, exp.RegexpReplace,
)
_NUM_ARG = (exp.Sum, exp.Avg, exp.Round, exp.Floor, exp.Ceil, exp.Sqrt, exp.Pow, exp.Abs, exp.Exp, exp.Ln, exp.Log)
_ARITH = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)
_COMPARE = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ)
_TIMESTAMP_ARG = (exp.UnixMicros, exp.UnixSeconds, exp.UnixMillis, exp.TimestampDiff, exp.TimestampTrunc, exp.TimestampAdd, exp.TimestampSub)
_DATEISH_ARG = (exp.DateDiff, exp.DateAdd, exp.DateSub, exp.DateTrunc, exp.LastDay, exp.TsOrDsToDate, exp.Date, exp.TimeToStr, exp.Extract)
_UNSUPPORTED_FUNCTIONS = ("ST_", "S2_", "KEYS.", "ML.", "AEAD.")


def _weak(kind: str | None) -> str | None:
    """A string literal compared to a column does not make it a STRING: dates coerce from strings."""
    return "STRING_LIT" if kind == "STRING" else kind


def lit_type(node: exp.Expression | None) -> str | None:
    """The type an expression has when it is obvious from its form, else None."""
    if node is None:
        return None
    if isinstance(node, exp.Paren):
        return lit_type(node.this)
    if isinstance(node, exp.Neg):
        return lit_type(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "STRING"
        text = node.name
        return "INT64" if re.fullmatch(r"\d+", text) else "FLOAT64"
    if isinstance(node, exp.Boolean):
        return "BOOL"
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return _TYPE_NAMES.get(node.to.this)
    if isinstance(node, exp.CurrentDate):
        return "DATE"
    if isinstance(node, exp.CurrentTimestamp):
        return "TIMESTAMP"
    if isinstance(node, (exp.Extract, exp.Count, exp.DateDiff, exp.TimestampDiff, exp.UnixMicros, exp.UnixSeconds)):
        return "INT64"
    if isinstance(node, (exp.Lower, exp.Upper, exp.Concat, exp.Substring, exp.Trim, exp.TimeToStr)):
        return "STRING"
    if isinstance(node, (exp.Date, exp.StrToDate, exp.DateTrunc, exp.DateAdd, exp.DateSub)):
        return "DATE"
    if isinstance(node, (exp.Timestamp,)):
        return "TIMESTAMP"
    if isinstance(node, exp.Div):
        return "FLOAT64"
    return None


class _Inferrer:
    def __init__(self, tree: exp.Expression) -> None:
        self.tree = tree
        self.tables: dict[str, Table] = {}
        self.unnests: dict[tuple[int, str], Node] = {}  # (scope id, alias) -> element node
        self.scopes: list[Scope] = []
        self.ambiguous: list[tuple[Node, list[str]]] = []

    # --- sources -----------------------------------------------------------------

    def physical(self, table: exp.Table) -> Table:
        key = table_key(table)
        if key not in self.tables:
            self.tables[key] = Table(key=key, local=f"t_{len(self.tables)}", wildcard="*" in table.name)
        self.tables[key].wildcard |= "*" in table.name
        return self.tables[key]

    @staticmethod
    def chain(scope: Scope | None):
        """The scope and, while it is a subquery or an UNNEST argument, the scopes it can see out of."""
        while scope is not None:
            yield scope
            scope = scope.parent if scope.scope_type in (ScopeType.SUBQUERY, ScopeType.UDTF) else None

    def lookup(self, scope: Scope | None, alias: str):
        for scope in self.chain(scope):
            source = scope.sources.get(alias)
            if source is not None:
                if isinstance(source, exp.Table):
                    return "table", self.physical(source)
                unnest = self.unnests.get((id(scope), alias))
                if unnest is not None:
                    return "unnest", unnest
                return "derived", source
        return None

    def nearest_table(self, scope: Scope | None) -> Table | None:
        for scope in self.chain(scope):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    return self.physical(source)
        return None

    def derived_outputs(self, scope: Scope) -> set[str]:
        out: set[str] = set()
        for s in self.chain(scope):
            for source in s.sources.values():
                if isinstance(source, Scope) and not isinstance(source.expression, exp.Unnest):
                    try:
                        out |= {n.lower() for n in source.expression.named_selects}
                    except Exception:  # noqa: BLE001
                        pass
        return out

    # --- resolving expressions to nodes ---------------------------------------------

    def resolve(self, node: exp.Expression | None, scope: Scope) -> Node | None:
        if node is None:
            return None
        if isinstance(node, exp.Paren):
            return self.resolve(node.this, scope)
        if isinstance(node, exp.Column):
            parts = [p.name for p in (node.args.get("db"), node.args.get("table"), node.this) if p is not None]
            return self.resolve_path(parts, scope, node)
        if isinstance(node, exp.Bracket):
            base = self.resolve(node.this, scope)
            return base.element() if base is not None else None
        if isinstance(node, exp.Dot):
            base = self.resolve(node.this, scope)
            name = node.expression.name if node.expression is not None else None
            return base.child(name) if base is not None and name else None
        return None

    def unnest_elems(self, scope: Scope) -> list[tuple[str, Node]]:
        out: list[tuple[str, Node]] = []
        for s in self.chain(scope):
            out += [(alias, node) for (sid, alias), node in self.unnests.items() if sid == id(s)]
        return out

    def derived_node(self, source: Scope, name: str, depth: int = 0) -> Node | None:
        """The node behind output column ``name`` of a derived table or CTE, if it is a plain column read."""
        if depth > 12:
            return None
        expr = source.expression
        if isinstance(expr, exp.Subquery):
            expr = expr.this
        if isinstance(expr, exp.Union):
            nodes = [self.derived_node(branch, name, depth + 1) for branch in source.set_operation_scopes]
            nodes = [n for n in nodes if n is not None]
            for other in nodes[1:]:
                link(nodes[0], other)
            return nodes[0] if nodes else None
        if not isinstance(expr, exp.Select):
            return None
        star = False
        for item in expr.expressions:
            if item.alias_or_name.lower() == name.lower() and not isinstance(item, exp.Star):
                inner = item.this if isinstance(item, exp.Alias) else item
                return self.resolve(inner, source)
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                star = True
        if star:
            return self.resolve_path([name], source)
        return None

    def derived_source_has(self, source: Scope, name: str) -> bool:
        expr = source.expression
        if isinstance(expr, exp.Subquery):
            expr = expr.this
        if isinstance(expr, exp.Union):
            expr = expr.left
            while isinstance(expr, (exp.Union, exp.Subquery)):
                expr = expr.left if isinstance(expr, exp.Union) else expr.this
        if not isinstance(expr, exp.Select):
            return False
        for item in expr.expressions:
            if item.alias_or_name.lower() == name.lower():
                return True
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                return True
        return False

    def resolve_path(self, parts: list[str], scope: Scope, column: exp.Column | None = None) -> Node | None:
        first = parts[0]
        shadowed = False
        if column is not None:  # UNNEST(hits) AS hits: inside the argument, hits is still the column
            holder = column.find_ancestor(exp.Unnest)
            shadowed = holder is not None and holder.alias == first
        found = None
        if not shadowed and (len(parts) > 1 or self._is_unnest_alias(scope, first)):
            found = self.lookup(scope, first)
        if found:
            kind, target = found
            if kind == "derived":
                if len(parts) == 1:
                    return None
                node = self.derived_node(target, parts[1])
                rest = parts[2:]
            elif kind == "unnest":
                node = target
                rest = parts[1:]
            else:
                if len(parts) == 1:
                    return None
                node = target.column(parts[1])
                rest = parts[2:]
            if node is None:
                return None
            for name in rest:
                node = node.child(name)
            return node
        rest = parts[1:]

        def done(node: Node | None) -> Node | None:
            if node is None:
                return None
            for name in rest:
                node = node.child(name)
            return node

        if column is not None and self._is_select_alias(column, scope, first):
            return None
        for s in self.chain(scope):  # a derived table that provides the name
            for source in s.sources.values():
                if isinstance(source, Scope) and not isinstance(source.expression, exp.Unnest) and self.derived_source_has(source, first):
                    return done(self.derived_node(source, first))
        for _, elem in self.unnest_elems(scope):  # a field of an unnested struct
            if first.lower() in elem.find().fields:
                return done(elem.child(first))
        table = self.nearest_table(scope)
        if table is not None and first.lower() in table.columns:
            return done(table.column(first))
        own_table = any(isinstance(v, exp.Table) for v in scope.sources.values())
        own_unnests = [n for sid_alias, n in self.unnests.items() if sid_alias[0] == id(scope)]
        if not own_table and own_unnests:
            return done(own_unnests[-1].child(first))
        if table is not None:
            return done(table.column(first))
        elems = self.unnest_elems(scope)
        return done(elems[-1][1].child(first)) if elems else None

    def _is_unnest_alias(self, scope: Scope, alias: str) -> bool:
        return any((id(s), alias) in self.unnests for s in self.chain(scope))

    @staticmethod
    def _is_select_alias(column: exp.Column, scope: Scope, name: str) -> bool:
        if not isinstance(scope.expression, exp.Select):
            return False
        aliased = {
            item.alias.lower()
            for item in scope.expression.expressions
            if isinstance(item, exp.Alias) and not (isinstance(item.this, exp.Column) and item.this.name.lower() == item.alias.lower())
        }
        if name.lower() not in aliased:
            return False
        node = column.parent
        while node is not None and node is not scope.expression:
            if isinstance(node, (exp.Group, exp.Order, exp.Having, exp.Qualify)):
                return True
            node = node.parent
        return False

    # --- passes --------------------------------------------------------------------------

    def run(self) -> Schema:
        self.scopes = list(traverse_scope(self.tree))
        if not self.scopes:
            raise Unsupported("no query scope")
        for scope in self.scopes:
            for alias, source in scope.sources.items():
                expression = getattr(source, "expression", None)
                if isinstance(expression, exp.Unnest):
                    base = self.resolve(expression.expressions[0], scope) if expression.expressions else None
                    if base is not None:
                        self.unnests[(id(scope), alias)] = base.element()
                    else:
                        self.unnests[(id(scope), alias)] = Node()
        for scope in self.scopes:
            for column in walk_in_scope(scope.expression):
                if isinstance(column, exp.Column):
                    self.resolve(column, scope)
        for scope in self.scopes:
            for node in walk_in_scope(scope.expression):
                self.evidence(node, scope)
        for table in self.tables.values():
            if table.wildcard:
                table.column("_TABLE_SUFFIX").votes.add("STRING")
        return Schema(self.tables, self.ambiguous)

    def vote(self, node: exp.Expression | None, kind: str | None, scope: Scope) -> None:
        if node is None or kind is None:
            return
        target = self.resolve(node, scope)
        if target is not None:
            target.find().votes.add(kind)

    def unify(self, a: exp.Expression | None, b: exp.Expression | None, scope: Scope) -> None:
        na, nb = self.resolve(a, scope), self.resolve(b, scope)
        if na is not None and nb is not None:
            link(na, nb)
        elif na is not None:
            self.vote(a, _weak(lit_type(b)), scope)
        elif nb is not None:
            self.vote(b, _weak(lit_type(a)), scope)

    def evidence(self, node: exp.Expression, scope: Scope) -> None:
        if isinstance(node, exp.Func) and isinstance(node, exp.Anonymous):
            name = node.name.upper()
            if name.startswith(_UNSUPPORTED_FUNCTIONS):
                raise Unsupported(f"function {name} is not supported by the emulator")
        if type(node).__name__.startswith("ST") and isinstance(node, exp.Func):
            raise Unsupported(f"geography function {type(node).__name__}")
        if isinstance(node, _COMPARE):
            self.unify(node.this, node.expression, scope)
        elif isinstance(node, exp.Between):
            self.unify(node.this, node.args.get("low"), scope)
            self.unify(node.this, node.args.get("high"), scope)
        elif isinstance(node, exp.In):
            for item in node.expressions:
                self.unify(node.this, item, scope)
        elif isinstance(node, _STRING_ARG):
            self.vote(node.this, "STRING", scope)
            if isinstance(node, (exp.Like, exp.ILike)):
                self.vote(node.expression, "STRING", scope)
            if isinstance(node, exp.Concat):
                for item in node.expressions:
                    self.vote(item, "STRING", scope)
        elif isinstance(node, (exp.StrToDate, exp.StrToTime)):
            self.vote(node.this, "STRING", scope)
            fmt = node.args.get("format")
            if isinstance(fmt, exp.Literal) and fmt.is_string:
                self.vote(node.this, "FMT:" + _python_format(fmt.name), scope)
            elif isinstance(node, exp.StrToDate):
                self.vote(node.this, "FMT:%Y-%m-%d", scope)
        elif isinstance(node, _NUM_ARG):
            self.vote(node.this, "FLOAT64" if isinstance(node, (exp.Sqrt, exp.Pow, exp.Exp, exp.Ln, exp.Log)) else "NUM", scope)
        elif isinstance(node, _ARITH):
            for a, b in ((node.this, node.expression), (node.expression, node.this)):
                other = lit_type(b)
                kind = "FLOAT64" if isinstance(node, exp.Div) or other == "FLOAT64" else "INT64" if isinstance(node, exp.Mod) else "NUM"
                if other in (None, "INT64", "FLOAT64") or self.resolve(b, scope) is not None:
                    self.vote(a, kind, scope)
            na, nb = self.resolve(node.this, scope), self.resolve(node.expression, scope)
            if na is not None and nb is not None:
                link(na, nb)
        elif isinstance(node, (exp.Coalesce, exp.Greatest, exp.Least)):
            items = [node.this, *node.expressions]
            for other in items[1:]:
                self.unify(items[0], other, scope)
        elif isinstance(node, exp.If):
            self.vote(node.this, "BOOL", scope)
            self.unify(node.args.get("true"), node.args.get("false"), scope)
        elif isinstance(node, exp.Case):
            for branch in node.args.get("ifs", []):
                if node.this is not None:
                    self.unify(node.this, branch.this, scope)
                else:
                    self.vote(branch.this, "BOOL", scope)
        elif isinstance(node, (exp.And, exp.Or, exp.Not)):
            for side in (node.this, node.args.get("expression")):
                if isinstance(side, exp.Column):
                    self.vote(side, "BOOL", scope)
        elif isinstance(node, exp.Where):
            if isinstance(node.this, exp.Column):
                self.vote(node.this, "BOOL", scope)
        elif isinstance(node, exp.Bracket):
            self.resolve(node, scope)
        elif isinstance(node, exp.UnixToTime):
            self.vote(node.this, "INT64", scope)
        elif isinstance(node, exp.Join) and node.args.get("using"):
            self._using(node, scope)
        elif isinstance(node, _TIMESTAMP_ARG):
            self.vote(node.this, "TIMESTAMP", scope)
            if isinstance(node, exp.TimestampDiff):
                self.vote(node.expression, "TIMESTAMP", scope)
        elif isinstance(node, _DATEISH_ARG):
            part = node.this if isinstance(node, exp.Extract) else None
            target = node.expression if isinstance(node, exp.Extract) else node.this
            if isinstance(node, exp.Extract) and str(part.name if part else "").upper() in ("HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND"):
                self.vote(target, "TIMESTAMP", scope)
            else:
                self._dateish(target, scope)
                if isinstance(node, exp.DateDiff):
                    self._dateish(node.expression, scope)
        elif isinstance(node, (exp.Cast, exp.TryCast)):
            target = _TYPE_NAMES.get(node.to.this)
            if target in ("INT64", "FLOAT64", "DATE", "TIMESTAMP", "DATETIME", "NUMERIC"):
                self.vote(node.this, "WEAK_STRING", scope)
                self.vote(node.this, "FMT:" + _CAST_FORMAT[target], scope)

    def _using(self, join: exp.Join, scope: Scope) -> None:
        sources = list(scope.sources.items())
        alias = join.alias_or_name
        right = scope.sources.get(alias)
        left = next((v for k, v in sources if k != alias and not (isinstance(v, Scope) and isinstance(v.expression, exp.Unnest))), None)
        for column in join.args["using"]:
            name = column.name
            nodes = []
            for source in (left, right):
                if isinstance(source, exp.Table):
                    nodes.append(self.physical(source).column(name))
                elif isinstance(source, Scope):
                    nodes.append(self.derived_node(source, name))
            nodes = [n for n in nodes if n is not None]
            for other in nodes[1:]:
                link(nodes[0], other)

    def _dateish(self, target: exp.Expression | None, scope: Scope) -> None:
        resolved = self.resolve(target, scope)
        if resolved is None:
            return
        resolved.find().votes.add("DATEISH")
        root = resolved.find()
        if not any(root is n.find() for n, _ in self.ambiguous):
            self.ambiguous.append((root, ["DATE", "TIMESTAMP", "DATETIME"]))


def _declared(tree_statements: list[exp.Expression]) -> tuple[dict[str, exp.Expression], exp.Expression]:
    """Split a script into its DECLARE defaults and the single query."""
    defaults: dict[str, exp.Expression] = {}
    query = None
    for statement in tree_statements:
        if statement is None or isinstance(statement, exp.Semicolon):
            continue
        if isinstance(statement, exp.Declare):
            for item in statement.expressions:
                default = item.args.get("default")
                names = item.this if isinstance(item.this, list) else [item.this]
                if default is None or not names or any(n is None for n in names):
                    raise Unsupported("DECLARE without a literal default")
                for name in names:
                    defaults[name.name.lower()] = default
            continue
        if isinstance(statement, (exp.Select, exp.Union, exp.Subquery)):
            if query is not None:
                raise Unsupported("more than one query in the script")
            query = statement
            continue
        raise Unsupported(f"{type(statement).__name__} statement in the script")
    if query is None:
        raise Unsupported("no query in the script")
    return defaults, query


@dataclass
class Prepared:
    """A gold query ready to run: local table names, a schema, nothing else needed."""

    sql: str  # BigQuery text with tables renamed to t_N and variables inlined
    tree: exp.Expression
    schema: Schema
    tables: dict[str, str]  # original table key -> local name


def prepare(sql: str) -> Prepared:
    """Parse a gold query, inline DECLAREd variables, infer schemas, rename tables to ``t_N``."""
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        raise Unsupported(f"parse error: {str(error)[:80]}") from None
    defaults, query = _declared(statements)
    if "temp function" in sql.lower() or "create temp" in sql.lower():
        raise Unsupported("temporary function")
    if defaults:
        def inline(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column) and not node.args.get("table") and node.name.lower() in defaults:
                return defaults[node.name.lower()].copy()
            return node

        query = query.transform(inline)
    tree = query.copy()
    inferrer = _Inferrer(tree)
    schema = inferrer.run()

    def rename(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and not _is_cte_reference(node, tree):
            table = schema.tables.get(table_key(node))
            if table is None:
                return node
            alias = node.args.get("alias")
            fresh = exp.Table(this=exp.to_identifier(table.local))
            if alias is not None:
                fresh.set("alias", alias.copy())
            elif "*" not in node.name:
                fresh.set("alias", exp.TableAlias(this=exp.to_identifier(node.name)))
            return fresh
        if isinstance(node, exp.Column) and node.name.upper() == "_TABLE_SUFFIX":
            return exp.column("tsuffix_", table=node.args.get("table").name if node.args.get("table") else None)
        return node

    local = tree.transform(rename)
    for table in schema.tables.values():
        if "_table_suffix" in table.columns:
            name, node = table.columns.pop("_table_suffix")
            table.columns["tsuffix_"] = ("tsuffix_", node)
    return Prepared(local.sql(dialect="bigquery"), local, schema, {t.key: t.local for t in schema.tables.values()})


def _is_cte_reference(table: exp.Table, tree: exp.Expression) -> bool:
    if table.args.get("db") or table.args.get("catalog"):
        return False
    names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return table.name in names
