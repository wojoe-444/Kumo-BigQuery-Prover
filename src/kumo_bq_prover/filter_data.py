"""Test data that satisfies a query's own filters, joins and UNNESTs.

Random data rarely makes a real analytic query return a row: the filters of one
table occurrence must hold *on the same row*, the join keys of two occurrences
must be equal, an UNNESTed array must hold an element that passes the filter on
its fields, and a ``HAVING COUNT(*) > 10`` needs eleven rows in one group. This
module reads the query and builds rows for exactly that.

How it works (``plan``):

1. **Anchors.** Every physical table occurrence in the query (the same table read
   by three CTEs is three anchors) and every ``UNNEST`` (an element anchor inside
   its parent row) is an anchor. A *variable* is one scalar field of one anchor.
2. **Atoms.** The conjuncts of every WHERE, JOIN ... ON and non-aggregate HAVING
   are rewritten over variables. A column read through a derived table or CTE is
   followed back to its anchor; an output that is an expression (``EXTRACT(YEAR
   FROM d) AS year``) is substituted into the atom, so ``year = 2023`` becomes a
   test on ``d``. ``a = b`` between two variables links them (they take one value);
   an atom with a subquery, an aggregate or a window is skipped.
3. **Solutions.** Variables that share atoms form a component. Candidate values
   come from the constants of the atoms (and their neighbours), the shapes
   functions need (a year for ``EXTRACT(YEAR ...)``, a suffix for ``ENDS_WITH``)
   and small generic pools. The emulator tests batches of candidate tuples, so any
   function it can evaluate works, and the passing tuples are kept.
4. **Databases (``FilterPlan.database``).** Each seed picks a few solutions per
   component and writes one row per anchor from them (an UNNEST anchor becomes an
   element of its parent's array), repeated enough times for a ``HAVING COUNT``
   threshold, next to random rows from ``nested_datagen``.

Nothing here has to be right for a result to be sound: the data only decides which
databases the oracle sees. A wrong guess means the query still returns nothing and
the pair stays as unproductive as before; it cannot produce a label.
"""

from __future__ import annotations

import datetime
import decimal
import math
import random
from dataclasses import dataclass, field
from typing import Iterable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope, walk_in_scope

from . import nested_datagen
from .datagen import literal
from .emulator import Client, QueryError
from .schema_infer import _CAST_FORMAT, _TYPE_NAMES, ColType, _python_format, type_sql

#: Candidate tuples tried per emulator call, and solutions kept per component.
BATCH = 300
SOLUTIONS = 40
#: Scenarios (coherent sets of rows) per database, and the largest group a HAVING COUNT may ask for.
SCENARIOS = 3
MAX_MULTIPLICITY = 200
#: A group size above this is asked for with one scenario only, to keep the databases small.
BIG_GROUP = 30


class _Skip(Exception):
    """An atom or reference this module cannot follow."""


# --------------------------------------------------------------------------------------
# anchors and variables


@dataclass(eq=False)
class Anchor:
    ident: int
    table: str  # physical table (local name) the row lives in
    parent: "Anchor | None" = None
    parent_path: tuple = ()  # path of the array inside the parent
    kind: ColType = "STRING"  # type of the whole anchor: the row struct, or the array's element
    prefix: tuple = ()  # (table, column, field, ...) key of this anchor for the generator's classes
    orphan: bool = False  # an UNNEST whose array could not be followed: nothing can be placed


def _lookup_field(kind: ColType, name: str):
    """(display name, type) of a field of a STRUCT type, case-insensitive."""
    if isinstance(kind, tuple) and kind[0] == "STRUCT":
        for field_name, field_type in kind[1]:
            if field_name.lower() == name.lower():
                return field_name, field_type
    return None


def type_at(anchor: Anchor, path: tuple) -> ColType | None:
    kind = anchor.kind
    for part in path:
        found = _lookup_field(kind, part)
        if found is None:
            return None
        kind = found[1]
    return kind


def canonical_path(anchor: Anchor, path: tuple) -> tuple:
    out, kind = [], anchor.kind
    for part in path:
        name, kind = _lookup_field(kind, part)
        out.append(name)
    return tuple(out)


@dataclass
class Var:
    key: tuple  # (anchor ident, path)
    anchor: Anchor
    path: tuple
    kind: str  # scalar type


@dataclass
class Component:
    roots: list[tuple]  # root keys of the variables
    atoms: list[exp.Expression]
    solutions: list[dict] = field(default_factory=list)  # root key -> value
    loose: list[dict] = field(default_factory=list)  # solutions of the atoms read straight off the table (not through a derived table)
    dropped: int = 0  # atoms that had to be left out to find solutions


@dataclass
class Threshold:
    anchors: set  # anchor idents whose rows are repeated
    count: int
    distinct: bool = False  # COUNT(DISTINCT x): repeating a row does not help, more scenarios do


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, (exp.Where, exp.Having)):
        return _conjuncts(node.this)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _parts(node: exp.Expression) -> list[str] | None:
    """Name parts of a column or dotted path; None for anything else."""
    if isinstance(node, exp.Column):
        return [p.name for p in (node.args.get("catalog"), node.args.get("db"), node.args.get("table"), node.this) if p is not None]
    if isinstance(node, exp.Dot):
        base = _parts(node.this)
        name = node.expression.name if node.expression is not None else None
        if base is not None and name:
            return base + [name]
    return None


class Planner:
    """Walks one query and collects anchors, variables and atoms.

    A CTE or derived table read twice is two *instances*: ``stops AS a`` and ``stops AS b`` over the same CTE need
    different rows (``a.name = 'x'`` and ``b.name = 'y'``). Every reference carries a context ``ctx``, the chain of
    (scope, alias) it was reached through, and an anchor is a table occurrence *in a context*.
    """

    #: Scope instances visited before the contexts are collapsed (a CTE chain read twice at each step grows as 2**depth).
    MAX_INSTANCES = 400

    def __init__(self, tree: exp.Expression, schema: dict[str, dict[str, ColType]]) -> None:
        self.tree = tree
        self.schema = {table: {name.lower(): (name, kind) for name, kind in cols.items()} for table, cols in schema.items()}
        self.raw_schema = schema
        self.anchors: dict[int, Anchor] = {}
        self._anchor_keys: dict[tuple, Anchor] = {}
        self.vars: dict[tuple, Var] = {}
        self.parent_of: dict[tuple, tuple] = {}
        self.atoms: list[tuple[exp.Expression, set, bool]] = []
        self._via_derived = False
        self.min_scenarios = 0  # an OFFSET needs that many groups
        self.diversify: set[tuple] = set()  # variables under COUNT(DISTINCT ...) > n: scenarios must take different values
        self.pin: set[tuple] = set()  # the GROUP BY variables of that count: scenarios must share one value
        self.thresholds: list[Threshold] = []
        self.skipped = 0
        self._visited: set = set()
        self.scopes = list(traverse_scope(tree))

    # --- anchors ------------------------------------------------------------------

    def _anchor(self, key: tuple, **fields) -> tuple[Anchor, bool]:
        if key in self._anchor_keys:
            return self._anchor_keys[key], False
        anchor = Anchor(len(self.anchors) + 1, **fields)
        self._anchor_keys[key] = anchor
        self.anchors[anchor.ident] = anchor
        return anchor, True

    def table_anchor(self, table: exp.Table, ctx: tuple) -> Anchor | None:
        name = table.name
        if name not in self.raw_schema:
            return None
        columns = self.raw_schema[name]
        anchor, _ = self._anchor((id(table), ctx), table=name, kind=("STRUCT", tuple(columns.items())), prefix=(name,))
        return anchor

    def unnest_anchor(self, source: Scope, owner: Scope, alias: str, ctx: tuple) -> Anchor:
        unnest = source.expression
        anchor, new = self._anchor((id(unnest), ctx), table="", orphan=True)
        if not new:
            return anchor
        arg = unnest.expressions[0] if unnest.expressions else None
        try:
            ref = self.reference(arg, owner, {alias}, ctx) if arg is not None else None
        except _Skip:
            ref = None
        if ref is not None and ref[0] == "var":
            parent, path = ref[1], ref[2]
            kind = type_at(parent, path)
            if isinstance(kind, tuple) and kind[0] == "ARRAY":
                anchor.table = parent.table
                anchor.parent = parent
                anchor.parent_path = canonical_path(parent, path)
                anchor.kind = kind[1]
                anchor.prefix = parent.prefix + anchor.parent_path
                anchor.orphan = parent.orphan
        return anchor

    # --- references -----------------------------------------------------------------

    @staticmethod
    def _chain(scope: Scope) -> Iterable[Scope]:
        while scope is not None:
            yield scope
            scope = scope.parent if scope.scope_type in (ScopeType.SUBQUERY, ScopeType.UDTF) else None

    @staticmethod
    def _srcs(scope: Scope) -> dict:
        """Alias -> source for what the scope reads in its FROM and JOINs (``scope.sources`` also holds sibling CTEs)."""
        return {alias: source for alias, (_, source) in scope.selected_sources.items()}

    def reference(self, node: exp.Expression, scope: Scope, exclude: frozenset | set = frozenset(), ctx: tuple = ()):
        """``("var", anchor, path)`` or ``("expr", expression, scope, ctx)`` for a column or dotted path, else None."""
        if isinstance(node, exp.Paren):
            return self.reference(node.this, scope, exclude, ctx)
        parts = _parts(node)
        if parts is None:
            return None
        return self._resolve(parts, scope, exclude, ctx)

    def _source_provides(self, source, name: str, owner: Scope, alias: str, ctx: tuple) -> bool:
        if isinstance(source, exp.Table):
            return source.name in self.schema and name.lower() in self.schema[source.name]
        if isinstance(source, Scope):
            if isinstance(source.expression, exp.Unnest):
                anchor = self.unnest_anchor(source, owner, alias, ctx)
                return not anchor.orphan and _lookup_field(anchor.kind, name) is not None
            expr = source.expression
            if isinstance(expr, exp.Subquery):
                expr = expr.this
            if isinstance(expr, exp.Union):
                branches = source.set_operation_scopes
                return bool(branches) and self._source_provides(branches[0], name, owner, alias, ctx)
            if isinstance(expr, exp.Select):
                for item in expr.expressions:
                    if item.alias_or_name.lower() == name.lower() and not isinstance(item, exp.Star):
                        return True
                    if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                        return True
        return False

    def _resolve(self, parts: list[str], scope: Scope, exclude, ctx: tuple) -> tuple | None:
        first = parts[0]
        if first not in exclude:
            for owner in self._chain(scope):
                source = self._srcs(owner).get(first)
                if source is None:
                    continue
                scalar_unnest = isinstance(source, Scope) and isinstance(source.expression, exp.Unnest)
                if len(parts) > 1 or scalar_unnest:
                    return self._into(source, owner, first, parts[1:], exclude, ctx)
                break
        matches = []
        for owner in self._chain(scope):
            for alias, source in self._srcs(owner).items():
                if alias in exclude:
                    continue
                if self._source_provides(source, first, owner, alias, ctx):
                    matches.append((alias, source, owner))
            if matches:
                break
        if matches:
            # several sources with the column: take the first (a real clash would be a USING join, and either choice
            # is only a guess for the data)
            alias, source, owner = matches[0]
            return self._into(source, owner, alias, parts, exclude, ctx)
        return None

    def _into(self, source, owner: Scope, alias: str, rest: list[str], exclude, ctx: tuple) -> tuple | None:
        if isinstance(source, exp.Table):
            anchor = self.table_anchor(source, ctx)
            if anchor is None or not rest or type_at(anchor, tuple(rest)) is None:
                return None
            return ("var", anchor, tuple(rest))
        if not isinstance(source, Scope):
            return None
        expr = source.expression
        if isinstance(expr, exp.Unnest):
            anchor = self.unnest_anchor(source, owner, alias, ctx)
            if type_at(anchor, tuple(rest)) is None:
                return None
            return ("var", anchor, tuple(rest))
        if isinstance(expr, exp.Subquery):
            expr = expr.this
        if isinstance(expr, exp.Union):
            branches = source.set_operation_scopes
            if not branches:
                return None
            return self._into(branches[0], owner, alias, rest, exclude, ctx)
        if not isinstance(expr, exp.Select) or not rest:
            return None
        inner_ctx = ctx + ((id(owner), alias),)
        name, tail = rest[0], rest[1:]
        self._via_derived = True
        star = False
        for item in expr.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                star = True
                continue
            if item.alias_or_name.lower() == name.lower():
                inner = item.this if isinstance(item, exp.Alias) else item
                if tail:
                    base = self.reference(inner, source, set(), inner_ctx)
                    if base is None or base[0] != "var":
                        return None
                    return ("var", base[1], base[2] + tuple(tail))
                if _parts(inner) is not None:  # a plain column or struct path read: follow it to its anchor
                    followed = self.reference(inner, source, set(), inner_ctx)
                    if followed is not None:
                        return followed
                return ("expr", inner, source, inner_ctx)
        if star:
            return self._resolve([name] + tail, source, set(), inner_ctx)
        return None

    def follow_var(self, node: exp.Expression, scope: Scope, ctx: tuple = (), depth: int = 0):
        """The (anchor, path) a column reads, looking through derived tables and MIN / MAX / ANY_VALUE of a column."""
        if depth > 12:
            return None
        while isinstance(node, (exp.Paren, exp.Min, exp.Max, exp.AnyValue, exp.Alias, exp.Cast, exp.TryCast)):
            node = node.this
        try:
            ref = self.reference(node, scope, set(), ctx)
        except _Skip:
            return None
        if ref is None:
            return None
        if ref[0] == "var":
            return ref[1], canonical_path(ref[1], ref[2])
        return self.follow_var(ref[1], ref[2], ref[3], depth + 1)

    def flavours(self) -> dict[tuple, str]:
        """STRING columns that the query parses or casts: the format their values must have (``(table, column, field, ...)`` keys)."""
        out: dict[tuple, str] = {}
        saved = (dict(self.anchors), dict(self._anchor_keys), dict(self.vars))
        for scope in self.scopes:
            for node in walk_in_scope(scope.expression):
                if isinstance(node, (exp.StrToDate, exp.StrToTime)):
                    fmt = node.args.get("format")
                    flavour = _python_format(fmt.name) if isinstance(fmt, exp.Literal) else "%Y-%m-%d"
                elif isinstance(node, (exp.Cast, exp.TryCast)):
                    target = _TYPE_NAMES.get(node.to.this)
                    if target not in _CAST_FORMAT:
                        continue
                    flavour = _CAST_FORMAT[target]
                else:
                    continue
                found = self.follow_var(node.this, scope)
                if found is None or type_at(found[0], found[1]) != "STRING":
                    continue
                out.setdefault(found[0].prefix + found[1], flavour)
        self.anchors, self._anchor_keys, self.vars = saved  # the scan made anchors in the wrong contexts: drop them
        return out

    # --- terms ------------------------------------------------------------------------

    def term(self, node: exp.Expression, scope: Scope, ctx: tuple = (), depth: int = 0) -> exp.Expression:
        """The expression with every column replaced by a placeholder ``s.v_<n>``; raises _Skip when it cannot be followed."""
        if depth > 40:
            raise _Skip("depth")
        if isinstance(node, (exp.Min, exp.Max, exp.AnyValue, exp.Sum, exp.Avg)) and not isinstance(node.this, exp.Distinct):
            # a group of one row: the aggregate is its argument (enough to build rows that pass a filter on it)
            return self.term(node.this, scope, ctx, depth + 1)
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select) and len(node.this.expressions) == 1:
            # (SELECT start_date FROM dates): a constant when the CTE it reads is a literal select
            for inner_scope in self.scopes:
                if inner_scope.expression is node.this:
                    inner = node.this.expressions[0]
                    inner = inner.this if isinstance(inner, exp.Alias) else inner
                    value = self.term(inner, inner_scope, ctx, depth + 1)
                    if any(self._is_var(c) for c in value.find_all(exp.Column)):
                        raise _Skip("subquery reads a table")
                    return value
            raise _Skip("subquery")
        if isinstance(node, (exp.Subquery, exp.Select, exp.Exists, exp.AggFunc, exp.Window, exp.Placeholder, exp.Star)):
            raise _Skip(type(node).__name__)
        if isinstance(node, (exp.Column, exp.Dot)):
            ref = self.reference(node, scope, set(), ctx)
            if ref is None:
                raise _Skip("unresolved")
            if ref[0] == "var":
                anchor, path = ref[1], ref[2]
                kind = type_at(anchor, path)
                if not isinstance(kind, str):
                    raise _Skip("not scalar")
                key = (anchor.ident, canonical_path(anchor, path))
                if key not in self.vars:
                    self.vars[key] = Var(key, anchor, key[1], kind)
                return exp.Column(this=exp.to_identifier(_name(key)), table=exp.to_identifier("s"))
            self._via_derived = True
            return self.term(ref[1], ref[2], ref[3], depth + 1)
        if not isinstance(node, exp.Expression):
            return node
        args = {}
        for key, value in node.args.items():
            if isinstance(value, exp.Expression):
                args[key] = self.term(value, scope, ctx, depth + 1)
            elif isinstance(value, list):
                args[key] = [self.term(v, scope, ctx, depth + 1) if isinstance(v, exp.Expression) else v for v in value]
            else:
                args[key] = value
        return node.__class__(**args)

    # --- the walk -------------------------------------------------------------------------

    def union(self, a: tuple, b: tuple) -> None:
        ra, rb = self.root(a), self.root(b)
        if ra != rb:
            self.parent_of[rb] = ra

    def root(self, key: tuple) -> tuple:
        while key in self.parent_of:
            key = self.parent_of[key]
        return key

    def walk(self) -> None:
        if self.scopes:
            self._visit(self.scopes[-1], ())

    def _visit(self, scope: Scope, ctx: tuple) -> None:
        key = (id(scope), ctx)
        if key in self._visited:
            return
        if len(self._visited) >= self.MAX_INSTANCES and ctx:
            ctx = ()  # too many instances: from here on, readings of one CTE share their rows
            key = (id(scope), ctx)
            if key in self._visited:
                return
        self._visited.add(key)
        expr = scope.expression
        if isinstance(expr, exp.Select):
            offset, limit = expr.args.get("offset"), expr.args.get("limit")
            if offset is not None and isinstance(offset.expression, exp.Literal):
                size = int(limit.expression.name) if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.name.isdigit() else 1
                self.min_scenarios = max(self.min_scenarios, int(offset.expression.name) + size + 3)
            conditions = _conjuncts(expr.args.get("where"))
            for join in expr.args.get("joins") or []:
                conditions += _conjuncts(join.args.get("on"))
                for ident in join.args.get("using") or []:
                    self._link_using(scope, ident.name, ctx)
            for condition in conditions:
                self._atom(condition, scope, ctx)
            for condition in _conjuncts(expr.args.get("having")):
                self._having(condition, scope, ctx)
        # every table and UNNEST gets an anchor, filtered or not: an unnested array needs an element
        for alias, source in self._srcs(scope).items():
            if isinstance(source, Scope) and isinstance(source.expression, exp.Unnest):
                self.unnest_anchor(source, scope, alias, ctx)
            elif isinstance(source, exp.Table):
                self.table_anchor(source, ctx)
        for alias, source in self._srcs(scope).items():
            if isinstance(source, Scope) and not isinstance(source.expression, exp.Unnest):
                self._visit(source, ctx + ((id(scope), alias),))
        for child in list(scope.subquery_scopes) + list(scope.set_operation_scopes):
            self._visit(child, ctx)

    def _link_using(self, scope: Scope, name: str, ctx: tuple) -> None:
        keys = []
        for alias, source in self._srcs(scope).items():
            if self._source_provides(source, name, scope, alias, ctx):
                try:
                    ref = self._into(source, scope, alias, [name], set(), ctx)
                except _Skip:
                    ref = None
                if ref and ref[0] == "var":
                    key = (ref[1].ident, canonical_path(ref[1], ref[2]))
                    kind = type_at(ref[1], ref[2])
                    if isinstance(kind, str):
                        self.vars.setdefault(key, Var(key, ref[1], key[1], kind))
                        keys.append(key)
        for other in keys[1:]:
            self.union(keys[0], other)

    def _atom(self, condition: exp.Expression, scope: Scope, ctx: tuple) -> None:
        if self._count_threshold(condition, scope, ctx):
            return
        self._via_derived = False
        if isinstance(condition, exp.EQ):
            try:
                left, right = self.term(condition.left, scope, ctx), self.term(condition.right, scope, ctx)
            except _Skip:
                self.skipped += 1
                self._rank_demand(condition, scope, ctx)
                return
            if self._is_var(left) and self._is_var(right):
                self.union(self._var_key(left), self._var_key(right))
                return
            self._add_atom(exp.EQ(this=left, expression=right))
            return
        try:
            tree = self.term(condition, scope, ctx)
        except _Skip:
            self.skipped += 1
            self._rank_demand(condition, scope, ctx)
            return
        self._add_atom(tree)

    def _rank_demand(self, condition: exp.Expression, scope: Scope, ctx: tuple) -> None:
        """``rank = 3`` over a ROW_NUMBER / RANK needs at least three groups: ask for more scenarios."""
        if not isinstance(condition, (exp.EQ, exp.GT, exp.GTE)):
            return
        left, right = condition.left, condition.right
        if not (isinstance(right, exp.Literal) and not right.is_string and right.name.isdigit()):
            return
        node, depth = left, 0
        while depth < 8:
            depth += 1
            while isinstance(node, exp.Paren):
                node = node.this
            if isinstance(node, exp.Window):
                if isinstance(node.this, (exp.RowNumber, exp.Rank, exp.DenseRank)):
                    need = int(right.name) + (1 if isinstance(condition, exp.GT) else 0)
                    self.min_scenarios = max(self.min_scenarios, need + 2)
                return
            if isinstance(node, (exp.Column, exp.Dot)):
                try:
                    ref = self.reference(node, scope, set(), ctx)
                except _Skip:
                    return
                if ref is None or ref[0] != "expr":
                    return
                node, scope, ctx = ref[1], ref[2], ref[3]
                continue
            return

    def _count_threshold(self, condition: exp.Expression, scope: Scope, ctx: tuple) -> bool:
        """``count_alias > n`` over a derived COUNT, or ``(SELECT COUNT(*) ...) > n``: remember that groups need n + 1 rows."""
        if not isinstance(condition, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            return False
        left, right = condition.left, condition.right
        if isinstance(condition, (exp.LT, exp.LTE)):
            left, right = right, left
        if not (isinstance(right, exp.Literal) and not right.is_string):
            return False
        try:
            bound = int(float(right.name))
        except ValueError:
            return False
        need = bound + 1 if isinstance(condition, (exp.GT, exp.LT)) else bound
        found = self._counting(left, scope, ctx)
        if found is None:
            return False
        anchors, distinct, targets, home = found
        if anchors and need > 1:
            self.thresholds.append(Threshold(anchors, min(need, MAX_MULTIPLICITY), distinct))
            if distinct:
                for target, target_scope, target_ctx in targets:
                    self._mark(self.diversify, target, target_scope, target_ctx)
                group = home[0].expression.args.get("group") if isinstance(home[0].expression, exp.Select) else None
                for key in (group.expressions if group is not None else []):
                    self._mark(self.pin, key, home[0], home[1])
        return True

    def _mark(self, into: set, node: exp.Expression, scope: Scope, ctx: tuple) -> None:
        try:
            placeholder = self.term(node, scope, ctx)
        except _Skip:
            return
        if self._is_var(placeholder):
            into.add(self._var_key(placeholder))

    def _counting(self, node: exp.Expression, scope: Scope, ctx: tuple, depth: int = 0):
        """(anchors read by the COUNT, is DISTINCT, the counted expressions) for a count, directly or through derived outputs."""
        if depth > 8:
            return None
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Count):
            return self._anchors_below(scope, ctx), isinstance(node.this, exp.Distinct), self._distinct_targets(node, scope, ctx), (scope, ctx)
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select) and len(node.this.expressions) == 1:
            inner = node.this.expressions[0]
            inner = inner.this if isinstance(inner, exp.Alias) else inner
            if isinstance(inner, exp.Count):
                for sc in self.scopes:
                    if sc.expression is node.this:
                        return self._anchors_below(sc, ctx), isinstance(inner.this, exp.Distinct), self._distinct_targets(inner, sc, ctx), (sc, ctx)
            return None
        if isinstance(node, (exp.Column, exp.Dot)):
            try:
                ref = self.reference(node, scope, set(), ctx)
            except _Skip:
                return None
            if ref is not None and ref[0] == "expr":
                return self._counting(ref[1], ref[2], ref[3], depth + 1)
        return None

    @staticmethod
    def _distinct_targets(count: exp.Count, scope: Scope, ctx: tuple) -> list:
        inner = count.this
        if isinstance(inner, exp.Distinct):
            return [(e, scope, ctx) for e in inner.expressions]
        return []

    def _is_var(self, node: exp.Expression) -> bool:
        return isinstance(node, exp.Column) and node.name.startswith("v_") and node.table == "s"

    def _var_key(self, node: exp.Column) -> tuple:
        for key in self.vars:
            if _name(key) == node.name:
                return key
        raise _Skip("var")

    def _add_atom(self, tree: exp.Expression) -> None:
        names = {c.name for c in tree.find_all(exp.Column) if self._is_var(c)}
        if not names:
            return
        keys = {_name(key): key for key in self.vars if _name(key) in names}
        self.atoms.append((tree, set(keys.values()), self._via_derived))

    def _having(self, condition: exp.Expression, scope: Scope, ctx: tuple) -> None:
        """``HAVING COUNT(...) > n`` asks for groups of n + 1 rows: remember it so the rows are repeated."""
        if isinstance(condition, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            left, right = (condition.right, condition.left) if isinstance(condition, (exp.LT, exp.LTE)) else (condition.left, condition.right)
            if isinstance(left, exp.Count) and isinstance(right, exp.Literal) and not right.is_string:
                if self._count_threshold(condition, scope, ctx):
                    return
        try:
            self._atom(condition, scope, ctx)
        except _Skip:
            self.skipped += 1

    def _anchors_below(self, scope: Scope, ctx: tuple, seen: set | None = None) -> set:
        seen = seen if seen is not None else set()
        out: set = set()
        for alias, source in self._srcs(scope).items():
            if isinstance(source, exp.Table):
                anchor = self.table_anchor(source, ctx)
                if anchor is not None:
                    out.add(anchor.ident)
            elif isinstance(source, Scope):
                if isinstance(source.expression, exp.Unnest):
                    out.add(self.unnest_anchor(source, scope, alias, ctx).ident)
                elif (id(source), ctx) not in seen:
                    seen.add((id(source), ctx))
                    out |= self._anchors_below(source, ctx + ((id(scope), alias),), seen)
        return out


def _share_formats(flavours: dict[tuple, str], classes: dict[tuple, int]) -> dict[tuple, str]:
    """Columns of one class share a value pool, so every column of a class with a parsed column gets its format."""
    by_class = {}
    for key, flavour in flavours.items():
        if key in classes:
            by_class.setdefault(classes[key], flavour)
    out = dict(flavours)
    for key, class_id in classes.items():
        if class_id in by_class:
            out.setdefault(key, by_class[class_id])
    return out


def _name(key: tuple) -> str:
    """The placeholder column that stands for the variable ``key`` (anchor ident, path) inside an atom."""
    return f"v_{key[0]}_{'_'.join(key[1])}"


# --------------------------------------------------------------------------------------
# candidate values


def _constants(atoms: Iterable[exp.Expression]) -> tuple[list[int], list[float], list[str]]:
    ints: list[int] = []
    floats: list[float] = []
    strings: list[str] = []
    for atom in atoms:
        for raw in atom.find_all(exp.RawString):  # r'...' patterns
            strings.append(raw.name)
        for literal_node in atom.find_all(exp.Literal):
            if literal_node.is_string:
                strings.append(literal_node.name)
                continue
            try:
                number = decimal.Decimal(literal_node.name)
            except decimal.InvalidOperation:
                continue
            if number == number.to_integral_value() and "." not in literal_node.name and "e" not in literal_node.name.lower():
                ints.append(int(number))
            else:
                floats.append(float(number))
    return list(dict.fromkeys(ints)), list(dict.fromkeys(floats)), list(dict.fromkeys(strings))


def _year_like(ints: list[int]) -> list[int]:
    return [n for n in ints if 1900 <= n <= 2100]


def _date_points(ints: list[int], strings: list[str]) -> list[datetime.date]:
    """Dates a filter on a date column could want: around the date constants, and built from year / month / day constants."""
    days: list[datetime.date] = []
    for text in strings:
        for width in (10, 8):
            try:
                day = datetime.date.fromisoformat(text[:10]) if width == 10 else datetime.datetime.strptime(text[:8], "%Y%m%d").date()
            except ValueError:
                continue
            for shift in (-1, 0, 1, 2, 3, 5, 7, 8, 10, 14, 30):
                days.append(day + datetime.timedelta(days=shift))
            break
        else:
            if text.isdigit() and len(text) == 6:
                try:
                    days.append(datetime.datetime.strptime(text + "15", "%Y%m%d").date())
                except ValueError:
                    pass
    years = _year_like(ints) or [2020, 2021, 2019]
    years = years + [y + 1 for y in years[:2]]
    months = sorted({n for n in ints if 1 <= n <= 12} | {1, 6, 12})
    mdays = sorted({n for n in ints if 1 <= n <= 28} | {1, 15})
    for year in years[:6]:
        for month in months[:6]:
            for day in mdays[:3]:
                days.append(datetime.date(year, month, day))
    return list(dict.fromkeys(days))


_REGEX_STEPS = [(r"\\d", "1"), (r"\\s", " "), (r"\\w", "a"), (r"\\.", "."), (r"\\", ""), (r"[0-9]+", "1"), (r"[a-z]+", "a"), (r".*", ""), (r".+", "a"), (".", "a"), ("+", ""), ("*", ""), ("?", ""), ("^", ""), ("$", ""), ("(?i)", "")]


def regex_samples(pattern: str) -> list[str]:
    """Strings a regular expression written as a constant plausibly matches: anchors, classes and groups removed."""
    if not any(c in pattern for c in "^$|()[]\\.*+?"):
        return []
    text = pattern
    for old, new in _REGEX_STEPS:
        text = text.replace(old, new)
    out = []
    for piece in text.replace("(", "").replace(")", "").split("|"):
        piece = piece.strip("[]")
        if piece:
            out.append(piece)
    return out


def _non_null(pool: list) -> list:
    return [v for v in pool if v is not None]


def _digits(n: int) -> str:
    """Numbers as strings the way date parts are stored: months and days are zero-padded."""
    return f"{n:02d}" if 0 <= n < 10 else str(n)


def candidates(kind: str, atoms: list[exp.Expression], rng: random.Random, flavour: str | None = None, limit: int = 90) -> list:
    """Typed candidate values for one variable, from the constants of the atoms it appears in."""
    ints, floats, strings = _constants(atoms)
    out: list = []
    if kind == "INT64":
        for n in ints + [int(f) for f in floats]:
            out += [n - 1, n, n + 1, n + 2, n * 2]
        if len(ints) >= 2:
            out.append((min(ints) + max(ints)) // 2)
        out += [0, 1, 2, 3, 4, 5, 6, 10, 12, 100] + _year_like(ints) + [2020, 2021]
        out += [int(s) for s in strings if s.lstrip("-").isdigit() and len(s) < 15]
        if any(isinstance(n, (exp.UnixToTime, exp.UnixSeconds, exp.UnixMillis, exp.UnixMicros)) or "TIMESTAMP_MICROS" in n.sql(dialect="bigquery") or "TIMESTAMP_MILLIS" in n.sql(dialect="bigquery") or "TIMESTAMP_SECONDS" in n.sql(dialect="bigquery") for a in atoms for n in a.find_all(exp.Func)):
            # epoch counts for the dates the query mentions
            epoch = datetime.datetime(1970, 1, 1)
            for day in _date_points(ints, strings)[:24]:
                seconds = int((datetime.datetime.combine(day, datetime.time(12)) - epoch).total_seconds())
                out += [seconds, seconds * 1000, seconds * 1_000_000]
    elif kind == "FLOAT64":
        for n in ints + floats:
            out += [float(n) - 1, float(n), float(n) + 0.5, float(n) + 1, float(n) * 2]
        out += [0.0, 1.0, 2.0, 0.5, 10.0, 100.0]
    elif kind in ("NUMERIC", "BIGNUMERIC"):
        for n in ints + floats:
            out += [str(decimal.Decimal(str(n)) + d) for d in (-1, 0, 1)]
        out += ["0", "1", "2", "10"]
    elif kind == "STRING":
        for s in strings:
            for sample in [s] + regex_samples(s):
                base = sample.replace("%", "").replace("_", "") or sample
                out += [sample, base, "x" + base, base + "x", "x" + base + "x", base.upper(), base.lower()]
            base = s.replace("%", "").replace("_", "") or s
            if base.isdigit():
                out += [str(int(base) + 1)]
        texts = [t.replace("%", "").replace("_", "") for t in strings if 2 < len(t) < 30 and not regex_samples(t)]
        texts = list(dict.fromkeys(t for t in texts if t))
        for i, first in enumerate(texts[:6]):  # one value that several LIKE / contains filters accept together
            for second in texts[i + 1 : 6]:
                out += [f"{first} {second}", f"{first},{second}"]
        if 2 < len(texts) <= 6:
            out += [" ".join(texts), ",".join(texts)]
        for i, low in enumerate(strings[:4]):  # a value between the bounds of a BETWEEN on strings
            for high in strings[i + 1 : 4]:
                if low.isdigit() and high.isdigit() and len(low) == len(high) and low < high:
                    out.append(str((int(low) + int(high)) // 2).zfill(len(low)))
        for n in ints:
            out += [_digits(n), _digits(n + 1), _digits(n - 1)]
        for n in floats:
            out += [str(n)]
        out += ["a", "b", "c", "x", "00", "01", "02", "03", "06", "12", "2020", "202011"]
        for day in _date_points(ints, strings)[:40]:
            out += [day.strftime("%Y-%m-%d"), day.strftime("%Y%m%d"), day.strftime("%Y-%m"), day.strftime("%Y")]
        if flavour:
            out += nested_datagen.flavour_pool(flavour)
            out = [v for v in out if isinstance(v, str) and nested_datagen._parses(flavour, v)]
    elif kind == "DATE":
        out += [d.isoformat() for d in _date_points(ints, strings)]
    elif kind == "DATETIME":
        out += [d.strftime("%Y-%m-%d") + t for d in _date_points(ints, strings) for t in (" 00:00:00", " 12:30:45")]
    elif kind == "TIMESTAMP":
        out += [d.strftime("%Y-%m-%d") + t for d in _date_points(ints, strings) for t in (" 00:00:00+00", " 12:30:45+00")]
    elif kind == "BOOL":
        out += [True, False]
    out = list(dict.fromkeys(out))
    if len(out) > limit:
        head = out[: limit // 2]
        tail = rng.sample(out[limit // 2 :], k=limit - limit // 2)
        out = head + tail
    return out + [None]  # IS NULL filters need a NULL


# --------------------------------------------------------------------------------------
# solving atoms on the emulator


def _cell(kind: str, value) -> str:
    if value is None:
        return f"CAST(NULL AS {kind})"
    if kind == "FLOAT64":
        value = float(value)
    if kind == "INT64":
        value = int(value)
    return literal(kind, value)


def _passing(client: Client, names: list[str], kinds: list[str], tuples: list[tuple], atoms: list[exp.Expression]) -> list[int]:
    """Indexes of the tuples for which every atom is true on the emulator."""
    if not tuples:
        return []
    where = " AND ".join(f"({a.sql(dialect='bigquery')})" for a in atoms) or "TRUE"
    rows = ", ".join(
        "STRUCT(" + ", ".join([f"{i} AS i"] + [f"{_cell(k, v)} AS {n}" for n, k, v in zip(names, kinds, row)]) + ")"
        for i, row in enumerate(tuples)
    )
    sql = f"SELECT s.i FROM UNNEST([{rows}]) AS s WHERE {where}"
    try:
        return [int(r[0]) for r in client.query(sql).rows]
    except QueryError:
        if len(tuples) == 1:
            return []
        half = len(tuples) // 2
        left = _passing(client, names, kinds, tuples[:half], atoms)
        right = _passing(client, names, kinds, tuples[half:], atoms)
        return left + [half + i for i in right]


def _order(names: list[str], atoms: list[exp.Expression]) -> list[int]:
    """Variable order for the search: start from the most constrained, then neighbours that share atoms."""
    sets = [{c.name for c in a.find_all(exp.Column)} for a in atoms]
    weight = {n: sum(n in st for st in sets) for n in names}
    left = sorted(range(len(names)), key=lambda i: -weight[names[i]])
    order = [left.pop(0)]
    while left:
        placed = {names[i] for i in order}
        left.sort(key=lambda i: (-sum(1 for st in sets if names[i] in st and st & placed), -weight[names[i]]))
        order.append(left.pop(0))
    return order


def _directed(client: Client, names: list[str], kinds: list[str], placed: list[int], position: int, partials: list[tuple], done: list[exp.Expression], sets: list) -> list[tuple]:
    """Tuples that satisfy an equality by construction.

    For ``f(new) = g(old)`` with ``old`` already assigned, evaluate ``g`` on each partial assignment and offer the
    result as the new variable's value: ``LEFT(CAST(a AS STRING), 5) = b`` is then met by ``b`` = the left five of ``a``.
    """
    out: list[tuple] = []
    if not partials or not placed:
        return out
    new_name = names[position]
    old_names = [names[i] for i in placed]
    for atom in done:
        if not isinstance(atom, exp.EQ):
            continue
        for new_side, old_side in ((atom.left, atom.right), (atom.right, atom.left)):
            new_vars = {c.name for c in new_side.find_all(exp.Column)}
            old_vars = {c.name for c in old_side.find_all(exp.Column)}
            if new_vars != {new_name} or not old_vars or not old_vars <= set(old_names):
                continue
            rows = ", ".join(
                "STRUCT(" + ", ".join([f"{i} AS i"] + [f"{_cell(kinds[placed[j]], value)} AS {old_names[j]}" for j, value in enumerate(partial)]) + ")"
                for i, partial in enumerate(partials)
            )
            sql = f"SELECT s.i, {old_side.sql(dialect='bigquery')} AS t FROM UNNEST([{rows}]) AS s"
            try:
                result = client.query(sql).rows
            except QueryError:
                continue
            kind = kinds[position]
            for i, target in result:
                if target is None or isinstance(target, bool):
                    continue
                if kind == "STRING" and isinstance(target, str) or kind == "INT64" and isinstance(target, int) or kind == "FLOAT64" and isinstance(target, (int, float)):
                    out.append((*partials[int(i)], target))
    return out


def solve_component(client: Client, var_info: list[tuple[str, str, list]], atoms: list[exp.Expression], rng: random.Random) -> tuple[list[tuple], int]:
    """Satisfying tuples (one value per variable, in ``var_info`` order) for the atoms.

    Single-variable atoms first cut each pool down. Then variables are added one at a time: every partial
    assignment is extended with candidate values and kept if the atoms that just became complete hold on the
    emulator. If a step leaves nothing, its atoms are dropped (counted) and the search goes on without them.
    """
    names = [n for n, _, _ in var_info]
    kinds = [k for _, k, _ in var_info]
    pools = [list(p) for _, _, p in var_info]
    sets = [{c.name for c in a.find_all(exp.Column)} for a in atoms]
    dropped = 0
    for index, name in enumerate(names):
        own = [a for a, st in zip(atoms, sets) if st == {name}]
        if own and pools[index]:
            ok = _passing(client, [name], [kinds[index]], [(v,) for v in pools[index]], own)
            pools[index] = [pools[index][i] for i in ok]
    order = _order(names, atoms)
    partials: list[tuple] = [()]
    placed: list[int] = []
    for position in order:
        if not pools[position]:
            pools[position] = list(var_info[position][2])[:6]  # nothing satisfied the unary atoms: go on with a few raw values
            dropped += sum(1 for st in sets if st == {names[position]})
        chosen_names = [names[i] for i in placed] + [names[position]]
        chosen_kinds = [kinds[i] for i in placed] + [kinds[position]]
        done = [a for a, st in zip(atoms, sets) if names[position] in st and st <= set(chosen_names) and len(st) > 1]
        product = [(*p, v) for p in partials for v in pools[position]]
        if len(product) > BATCH:
            product = rng.sample(product, BATCH)
        product = _directed(client, names, kinds, placed, position, partials, done, sets) + product
        if done:
            ok = _passing(client, chosen_names, chosen_kinds, product, done)
            if not ok:
                dropped += len(done)
                ok = list(range(len(product)))
            product = [product[i] for i in ok]
        rng.shuffle(product)
        partials = product[:SOLUTIONS]
        placed.append(position)
    # partial tuples are in placement order; put them back in var_info order
    ordered = []
    for tup in partials:
        values = [None] * len(names)
        for slot, position in enumerate(placed):
            values[position] = tup[slot]
        ordered.append(tuple(values))
    if dropped and len(atoms) == 1:
        return [], dropped
    return ordered, dropped


# --------------------------------------------------------------------------------------
# the plan and the databases


@dataclass
class FilterPlan:
    schema: dict[str, dict[str, ColType]]
    anchors: dict[int, Anchor]
    vars: dict[tuple, Var]
    root_of: dict[tuple, tuple]
    components: list[Component]
    thresholds: list[Threshold]
    skipped: int
    notes: list[str] = field(default_factory=list)
    flavours: dict[tuple, str] = field(default_factory=dict)
    repeat: dict[int, int] = field(default_factory=dict)  # anchor ident -> copies of its row (or array element)
    scenarios: int = SCENARIOS
    diversify: set = field(default_factory=set)  # root keys whose values must differ between scenarios (COUNT(DISTINCT ...))
    pin: set = field(default_factory=set)  # root keys whose value must be the same in every scenario (the groups counted)

    def solvable(self) -> bool:
        return any(c.solutions for c in self.components)

    def summary(self) -> dict:
        """Counts for the results file: what was found, and what was not."""
        return {
            "anchors": len([a for a in self.anchors.values() if not a.orphan]),
            "variables": len(self.vars),
            "components": len(self.components),
            "components_solved": sum(1 for c in self.components if c.solutions),
            "atoms_dropped": sum(c.dropped for c in self.components),
            "atoms_skipped": self.skipped,
            "count_thresholds": len(self.thresholds),
            "scenarios": self.scenarios,
        }

    # --- rows -------------------------------------------------------------------------

    def _anchor_children(self, anchor: Anchor, path: tuple) -> list[Anchor]:
        return [a for a in self.anchors.values() if a.parent is anchor and a.parent_path == path and not a.orphan]

    def _place(self, gen, anchor: Anchor, path: tuple, kind: ColType, scenario: dict, rng: random.Random) -> str:
        key_path = anchor.prefix + path
        leaf = path[-1] if path else (anchor.prefix[-1] if anchor.prefix else "")
        if isinstance(kind, str):
            var_key = (anchor.ident, path)
            root = self.root_of.get(var_key)
            if root is not None and root in scenario:
                return _cell(kind, scenario[root])
            return gen.scalar(key_path, leaf, kind)
        if kind[0] == "STRUCT":
            fields = ", ".join(f"{self._place(gen, anchor, path + (name,), t, scenario, rng)} AS {name}" for name, t in kind[1])
            return f"STRUCT({fields})"
        items = []
        for child in self._anchor_children(anchor, path):
            items += [self._element(gen, child, scenario, rng)] * self.repeat.get(child.ident, 1)
        extras = rng.choice([0, 0, 1, 1, 2])
        items += [gen.value(key_path, leaf, kind[1]) for _ in range(extras)]
        if not items and rng.random() < 0.5:
            items = [gen.value(key_path, leaf, kind[1])]
        rng.shuffle(items)
        return f"{type_sql(kind)}[{', '.join(items)}]"

    def _element(self, gen, child: Anchor, scenario: dict, rng: random.Random) -> str:
        return self._place(gen, child, (), child.kind, scenario, rng)

    def _row(self, gen, anchor: Anchor, scenario: dict, rng: random.Random) -> str:
        cells = [self._place(gen, anchor, (name,), kind, scenario, rng) for name, kind in self.schema[anchor.table].items()]
        return "(" + ", ".join(cells) + ")"

    def database(self, seed: int, extra=None, classes=None, exact=None, flavours=None, noise_rows: int = 4) -> list[str]:
        merged = {**(flavours or {}), **self.flavours}
        gen = nested_datagen._Generator(seed, extra or {}, classes or {}, exact or {}, _share_formats(merged, classes or {}))
        rng = gen.rng
        statements = nested_datagen.create_statements(self.schema)
        rows: dict[str, list[str]] = {table: [] for table in self.schema}
        table_anchors = [a for a in self.anchors.values() if a.parent is None and not a.orphan and a.table in self.schema]
        # each component works from a few of its solutions per database, so scenarios collide on group and join keys;
        # a component under COUNT(DISTINCT ...) keeps all of them and takes a new value each time
        keep = max(3, min(self.scenarios - 1, 8))
        strict_pool, loose_pool, distinct_roots = {}, {}, {}
        for i, c in enumerate(self.components):
            wanted = [r for r in c.roots if r in self.diversify]
            if wanted:
                distinct_roots[i] = wanted
            size = 1 if any(r in self.pin for r in c.roots) else keep
            if c.solutions:
                strict_pool[i] = list(c.solutions) if wanted else rng.sample(c.solutions, min(size, len(c.solutions)))
            if c.loose:
                loose_pool[i] = list(c.loose) if wanted else rng.sample(c.loose, min(size, len(c.loose)))
        used: dict[tuple, set] = {}
        for index in range(self.scenarios):
            scenario: dict = {}
            loose = index % 2 == 1
            for i, component in enumerate(self.components):
                pool = loose_pool.get(i) if loose and i in loose_pool else strict_pool.get(i)
                if not pool:
                    continue
                if i in distinct_roots:
                    fresh = [sol for sol in pool if all(sol.get(r) not in used.get(r, ()) for r in distinct_roots[i])]
                    pool = fresh or pool
                choice = rng.choice(pool)
                for r in distinct_roots.get(i, ()):
                    used.setdefault(r, set()).add(choice.get(r))
                scenario.update(choice)
            for anchor in table_anchors:
                row = self._row(gen, anchor, scenario, rng)
                rows[anchor.table] += [row] * self.repeat.get(anchor.ident, 1)
        for table, columns in self.schema.items():
            for _ in range(rng.randint(0, noise_rows)):
                rows[table].append("(" + ", ".join(gen.value((table, name), name, kind) for name, kind in columns.items()) + ")")
            if rows[table]:
                statements.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join(rows[table])}")
        return statements


def plan(client: Client, tree: exp.Expression, schema: dict[str, dict[str, ColType]], flavours: dict[tuple, str] | None = None, seed: int = 0) -> FilterPlan:
    """Read the query's filters and solve them on the emulator; the result builds databases (``FilterPlan.database``)."""
    planner = Planner(tree, schema)
    planner.walk()
    flavours = {**(flavours or {}), **planner.flavours()}
    rng = random.Random(seed)
    # variables linked by equality share one root
    root_of = {key: planner.root(key) for key in planner.vars}
    # components over roots: roots that share an atom, plus linked groups with more than one member
    parent: dict[tuple, tuple] = {}

    def find(key):
        while key in parent:
            key = parent[key]
        return key

    atoms: list[tuple[exp.Expression, set, bool]] = []
    for tree_atom, keys, outer in planner.atoms:
        roots = {root_of[k] for k in keys if k in root_of}
        if not roots:
            continue
        # replace each variable's placeholder by its root's placeholder so linked variables are one
        mapping = {}
        for key in keys:
            mapping[_name(key)] = _name(root_of[key])

        def rename(node, mapping=mapping):
            if isinstance(node, exp.Column) and node.table == "s" and node.name in mapping:
                return exp.Column(this=exp.to_identifier(mapping[node.name]), table=exp.to_identifier("s"))
            return node

        renamed = tree_atom.transform(rename)
        atoms.append((renamed, roots, outer))
        ordered = sorted(roots)
        for other in ordered[1:]:
            ra, rb = find(ordered[0]), find(other)
            if ra != rb:
                parent[rb] = ra
    groups: dict[tuple, list[tuple]] = {}
    for key, root in root_of.items():
        groups.setdefault(root, []).append(key)
    linked_roots = {root for root, members in groups.items() if len(members) > 1}
    component_roots: dict[tuple, set] = {}
    for _, roots, _ in atoms:
        component_roots.setdefault(find(sorted(roots)[0]), set()).update(roots)
    for root in linked_roots:
        component_roots.setdefault(find(root), set()).add(root)
    diversify = {root_of[k] for k in planner.diversify if k in root_of}
    pin = {root_of[k] for k in planner.pin if k in root_of}
    for root in diversify | pin:
        component_roots.setdefault(find(root), set()).add(root)
    components: list[Component] = []
    notes: list[str] = []
    for roots in component_roots.values():
        ordered = sorted(roots)
        comp_atoms = [a for a, rs, _ in atoms if rs & roots]
        loose_atoms = [a for a, rs, outer in atoms if rs & roots and not outer]
        var_info = []
        for root in ordered:
            members = groups.get(root, [root])
            var = planner.vars[root]
            member_atoms = [a for a, rs, _ in atoms if root in rs]
            flavour = None
            for member in members:
                v = planner.vars[member]
                flavour = flavour or flavours.get(v.anchor.prefix + v.path)
            pool = candidates(var.kind, member_atoms, rng, flavour=flavour)
            var_info.append((_name(root), var.kind, pool))
        component = Component(ordered, comp_atoms)
        if not comp_atoms:
            tuples = [tuple(rng.choice(_non_null(p)) for _, _, p in var_info) for _ in range(SOLUTIONS)] if all(_non_null(p) for _, _, p in var_info) else []
            dropped = 0
        else:
            tuples, dropped = solve_component(client, var_info, comp_atoms, rng)
            if len(loose_atoms) < len(comp_atoms):
                # atoms read through a derived table (a filter on a year after a window, say) may hold only for the
                # last stage: databases built from the others as well keep earlier-stage rows, such as a previous year
                if loose_atoms:
                    loose_tuples, _ = solve_component(client, var_info, loose_atoms, rng)
                elif all(_non_null(p) for _, _, p in var_info):
                    loose_tuples = [tuple(rng.choice(_non_null(p)) for _, _, p in var_info) for _ in range(SOLUTIONS)]
                else:
                    loose_tuples = []
                for values in loose_tuples:
                    component.loose.append(dict(zip(ordered, values)))
        component.dropped = dropped
        for values in tuples:
            component.solutions.append(dict(zip(ordered, values)))
        if not component.solutions:
            notes.append(f"no solution for {len(comp_atoms)} atoms over {len(ordered)} variables")
        components.append(component)
    repeat: dict[int, int] = {}
    scenarios = max(SCENARIOS, min(planner.min_scenarios, 12))
    for threshold in planner.thresholds:
        if threshold.distinct:
            scenarios = max(scenarios, min(threshold.count + 1, 12))
            continue
        per_anchor = threshold.count if len(threshold.anchors) == 1 else math.ceil(threshold.count ** (1 / len(threshold.anchors))) + 1
        for ident in threshold.anchors:
            repeat[ident] = max(repeat.get(ident, 1), per_anchor)
    if repeat and max(repeat.values()) > BIG_GROUP:
        scenarios = 2
    return FilterPlan(schema, planner.anchors, planner.vars, root_of, components, planner.thresholds, planner.skipped, notes, flavours, repeat, scenarios, diversify, pin)
