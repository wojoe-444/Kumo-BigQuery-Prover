"""Make query pairs from one query: rewrites that keep its meaning, and mutations that may not.

*Rewrites* are equivalent by construction: each rule is a textbook identity of
GoogleSQL (``x BETWEEN a AND b`` is ``x >= a AND x <= b``, ``IF(c, a, b)`` is a
CASE, a CTE rename). They are labelled ``equivalent`` without running anything.
The oracle then tries to refute them; a refutation is a bug to look at (the
emulator's, the rule's, or a query whose result depends on row order or ties
under LIMIT), never a label to drop.

*Mutations* are small edits that usually change the result (``>`` to ``>=``,
``AND`` to ``OR``, a dropped condition, INNER to LEFT JOIN). A mutation is not
known to differ: some are harmless on any data. Its label comes from the
emulator, which either finds a database where the two queries return different
rows (``not_equivalent``, with that database kept as the witness) or does not
(``unlabelled``).

Everything works on the sqlglot tree of a BigQuery query and renders back with
``dialect="bigquery"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlglot import exp

_NONDETERMINISTIC = (exp.Rand, exp.Uuid) if hasattr(exp, "Uuid") else (exp.Rand,)


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str  # "equivalent" or "mutation"
    match: Callable[[exp.Expression], bool]
    build: Callable[[exp.Expression], exp.Expression | None]
    note: str = ""


def _deterministic(node: exp.Expression) -> bool:
    return not any(isinstance(n, _NONDETERMINISTIC) for n in node.walk())


def _has_subquery(node: exp.Expression) -> bool:
    return any(isinstance(n, (exp.Select, exp.Subquery)) for n in node.walk())


# --- equivalent rewrites ------------------------------------------------------------------------------

def _commute_eq(node: exp.EQ) -> exp.Expression:
    return exp.EQ(this=node.expression.copy(), expression=node.this.copy())


def _commute_and(node: exp.And) -> exp.Expression:
    return exp.And(this=node.expression.copy(), expression=node.this.copy())


def _neq_as_not_eq(node: exp.NEQ) -> exp.Expression:
    return exp.Not(this=exp.Paren(this=exp.EQ(this=node.this.copy(), expression=node.expression.copy())))


def _between_expand(node: exp.Between) -> exp.Expression:
    x = node.this
    return exp.Paren(
        this=exp.And(
            this=exp.GTE(this=x.copy(), expression=node.args["low"].copy()),
            expression=exp.LTE(this=x.copy(), expression=node.args["high"].copy()),
        )
    )


def _in_expand(node: exp.In) -> exp.Expression:
    parts = [exp.EQ(this=node.this.copy(), expression=item.copy()) for item in node.expressions]
    out = parts[0]
    for part in parts[1:]:
        out = exp.Or(this=out, expression=part)
    return exp.Paren(this=out)


def _coalesce_as_ifnull(node: exp.Coalesce) -> exp.Expression:
    return exp.Anonymous(this="IFNULL", expressions=[node.this.copy(), node.expressions[0].copy()])


def _if_as_case(node: exp.If) -> exp.Expression:
    return exp.Case(
        ifs=[exp.If(this=node.this.copy(), true=node.args["true"].copy())],
        default=node.args["false"].copy() if node.args.get("false") is not None else None,
    )


def _case_single_as_if(node: exp.Case) -> exp.Expression:
    branch = node.args["ifs"][0]
    return exp.If(this=branch.this.copy(), true=branch.args["true"].copy(), false=node.args["default"].copy())


def _case_add_else_null(node: exp.Case) -> exp.Expression:
    fresh = node.copy()
    fresh.set("default", exp.Null())
    return fresh


def _count_star_as_one(node: exp.Count) -> exp.Expression:
    return exp.Count(this=exp.Literal.number(1))


def _is_plain_case(node: exp.Case) -> bool:
    return node.this is None and len(node.args.get("ifs", [])) == 1 and node.args.get("default") is not None


EQUIVALENT_RULES = [
    Rule("commute-equality", "equivalent", lambda n: isinstance(n, exp.EQ), _commute_eq, "a = b is b = a"),
    Rule("commute-and", "equivalent", lambda n: isinstance(n, exp.And), _commute_and, "AND is commutative (no short-circuit is promised)"),
    Rule("not-equal-as-not-equal-to", "equivalent", lambda n: isinstance(n, exp.NEQ), _neq_as_not_eq, "a <> b is NOT (a = b), NULL included"),
    Rule("between-expanded", "equivalent", lambda n: isinstance(n, exp.Between) and _deterministic(n.this), _between_expand, "x BETWEEN a AND b is x >= a AND x <= b"),
    Rule(
        "in-list-as-or",
        "equivalent",
        lambda n: isinstance(n, exp.In) and len(n.expressions) >= 2 and not n.args.get("query") and not n.args.get("unnest") and _deterministic(n.this) and not _has_subquery(n),
        _in_expand,
        "x IN (a, b) is x = a OR x = b, NULL included",
    ),
    Rule("coalesce-as-ifnull", "equivalent", lambda n: isinstance(n, exp.Coalesce) and len(n.expressions) == 1, _coalesce_as_ifnull, "COALESCE(a, b) is IFNULL(a, b)"),
    Rule("if-as-case", "equivalent", lambda n: isinstance(n, exp.If) and not isinstance(n.parent, exp.Case) and n.args.get("true") is not None, _if_as_case, "IF(c, a, b) is CASE WHEN c THEN a ELSE b END"),
    Rule("case-as-if", "equivalent", _is_plain_case, _case_single_as_if, "a one-branch CASE with ELSE is IF"),
    Rule(
        "case-else-null",
        "equivalent",
        lambda n: isinstance(n, exp.Case) and n.args.get("default") is None,
        _case_add_else_null,
        "a CASE without ELSE returns NULL",
    ),
    Rule("count-star-as-count-one", "equivalent", lambda n: isinstance(n, exp.Count) and isinstance(n.this, exp.Star), _count_star_as_one, "COUNT(*) is COUNT(1)"),
]


def _wrap_in_cte(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, (exp.Select, exp.Union)):
        return None
    inner = tree.copy()
    return exp.select("*").from_("q_wrapped_").with_("q_wrapped_", as_=inner)


def _rename_ctes(tree: exp.Expression) -> exp.Expression | None:
    names = [cte.alias for cte in tree.find_all(exp.CTE)]
    if not names:
        return None
    fresh = tree.copy()
    mapping = {name: f"{name}_cte" for name in names}
    if len(set(mapping.values()) & set(names)):
        return None
    for cte in fresh.find_all(exp.CTE):
        cte.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[cte.alias])))
    for table in fresh.find_all(exp.Table):
        if not table.args.get("db") and not table.args.get("catalog") and table.name in mapping:
            if table.args.get("alias") is None:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set("this", exp.to_identifier(mapping[table.name]))
    return fresh


def _distinct_as_group_by(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, exp.Select) or not tree.args.get("distinct"):
        return None
    if tree.args.get("group") or tree.args.get("having") or tree.args.get("qualify"):
        return None
    if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Star)) for e in tree.expressions for n in e.walk()):
        return None
    fresh = tree.copy()
    fresh.set("distinct", None)
    fresh.set("group", exp.Group(expressions=[exp.Literal.number(i + 1) for i in range(len(fresh.expressions))]))
    return fresh


ROOT_REWRITES: list[tuple[str, Callable[[exp.Expression], exp.Expression | None]]] = [
    ("wrap-in-cte", _wrap_in_cte),
    ("rename-ctes", _rename_ctes),
    ("distinct-as-group-by", _distinct_as_group_by),
]


# --- mutations ---------------------------------------------------------------------------------------------

_BOUNDARY_FLIP = {exp.GT: exp.GTE, exp.GTE: exp.GT, exp.LT: exp.LTE, exp.LTE: exp.LT}


def _flip_boundary(node: exp.Expression) -> exp.Expression:
    return _BOUNDARY_FLIP[type(node)](this=node.this.copy(), expression=node.expression.copy())


def _flip_direction(node: exp.Expression) -> exp.Expression:
    flipped = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}[type(node)]
    return flipped(this=node.this.copy(), expression=node.expression.copy())


def _eq_as_neq(node: exp.EQ) -> exp.Expression:
    return exp.NEQ(this=node.this.copy(), expression=node.expression.copy())


def _and_as_or(node: exp.And) -> exp.Expression:
    return exp.Paren(this=exp.Or(this=node.this.copy(), expression=node.expression.copy()))


def _or_as_and(node: exp.Or) -> exp.Expression:
    return exp.Paren(this=exp.And(this=node.this.copy(), expression=node.expression.copy()))


def _drop_right_conjunct(node: exp.And) -> exp.Expression:
    return node.this.copy()


def _drop_left_conjunct(node: exp.And) -> exp.Expression:
    return node.expression.copy()


def _bump_int(node: exp.Literal) -> exp.Expression:
    return exp.Literal.number(int(node.name) + 1)


def _extend_string(node: exp.Literal) -> exp.Expression:
    return exp.Literal.string(node.name + "x")


def _join_inner_to_left(node: exp.Join) -> exp.Expression:
    fresh = node.copy()
    fresh.set("side", "LEFT")
    fresh.set("kind", None)
    return fresh


def _join_left_to_inner(node: exp.Join) -> exp.Expression:
    fresh = node.copy()
    fresh.set("side", None)
    return fresh


def _union_all_to_distinct(node: exp.Union) -> exp.Expression:
    fresh = node.copy()
    fresh.set("distinct", True)
    return fresh


def _toggle_distinct(node: exp.Select) -> exp.Expression:
    fresh = node.copy()
    fresh.set("distinct", None if node.args.get("distinct") else exp.Distinct())
    return fresh


def _count_distinct_to_count(node: exp.Count) -> exp.Expression:
    return exp.Count(this=node.this.expressions[0].copy())


def _count_to_count_distinct(node: exp.Count) -> exp.Expression:
    return exp.Count(this=exp.Distinct(expressions=[node.this.copy()]))


_AGG_SWAP = {exp.Max: exp.Min, exp.Min: exp.Max, exp.Sum: exp.Max, exp.Avg: exp.Max}


def _swap_aggregate(node: exp.Expression) -> exp.Expression:
    return _AGG_SWAP[type(node)](this=node.this.copy())


def _flip_order(node: exp.Ordered) -> exp.Expression:
    fresh = node.copy()
    fresh.set("desc", not node.args.get("desc"))
    return fresh


def _bump_limit(node: exp.Limit) -> exp.Expression:
    return exp.Limit(expression=exp.Literal.number(int(node.expression.name) + 1))


def _drop_not(node: exp.Not) -> exp.Expression:
    return node.this.copy()


def _drop_in_item(node: exp.In) -> exp.Expression:
    fresh = node.copy()
    fresh.set("expressions", [item.copy() for item in node.expressions[1:]])
    return fresh


def _is_null_flip(node: exp.Is) -> exp.Expression:
    return exp.Not(this=node.copy())


def _int_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit() and isinstance(node.parent, (*_BOUNDARY_FLIP, exp.EQ, exp.Between))


def _string_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and node.is_string and isinstance(node.parent, (exp.EQ, exp.In)) and len(node.name) > 0


MUTATIONS = [
    Rule("flip-boundary", "mutation", lambda n: type(n) in _BOUNDARY_FLIP, _flip_boundary, "> becomes >=, < becomes <="),
    Rule("flip-direction", "mutation", lambda n: type(n) in _BOUNDARY_FLIP, _flip_direction, "> becomes <"),
    Rule("equal-as-not-equal", "mutation", lambda n: isinstance(n, exp.EQ), _eq_as_neq, "= becomes <>"),
    Rule("and-as-or", "mutation", lambda n: isinstance(n, exp.And), _and_as_or),
    Rule("or-as-and", "mutation", lambda n: isinstance(n, exp.Or), _or_as_and),
    Rule("drop-right-conjunct", "mutation", lambda n: isinstance(n, exp.And), _drop_right_conjunct),
    Rule("drop-left-conjunct", "mutation", lambda n: isinstance(n, exp.And), _drop_left_conjunct),
    Rule("bump-integer-constant", "mutation", _int_literal, _bump_int),
    Rule("extend-string-constant", "mutation", _string_literal, _extend_string),
    Rule("inner-join-as-left-join", "mutation", lambda n: isinstance(n, exp.Join) and not n.args.get("side") and not n.args.get("kind") == "CROSS" and n.args.get("on") is not None, _join_inner_to_left),
    Rule("left-join-as-inner-join", "mutation", lambda n: isinstance(n, exp.Join) and n.args.get("side") == "LEFT" and n.args.get("on") is not None, _join_left_to_inner),
    Rule("union-all-as-union-distinct", "mutation", lambda n: isinstance(n, exp.Union) and not n.args.get("distinct"), _union_all_to_distinct),
    Rule("toggle-distinct", "mutation", lambda n: isinstance(n, exp.Select), _toggle_distinct),
    Rule("count-distinct-as-count", "mutation", lambda n: isinstance(n, exp.Count) and isinstance(n.this, exp.Distinct) and len(n.this.expressions) == 1, _count_distinct_to_count),
    Rule("count-as-count-distinct", "mutation", lambda n: isinstance(n, exp.Count) and n.this is not None and not isinstance(n.this, (exp.Star, exp.Distinct)), _count_to_count_distinct),
    Rule("swap-aggregate", "mutation", lambda n: type(n) in _AGG_SWAP and not isinstance(n.parent, exp.Window), _swap_aggregate, "MAX/MIN swapped, SUM/AVG to MAX"),
    Rule("flip-order-direction", "mutation", lambda n: isinstance(n, exp.Ordered) and not isinstance(n.parent, exp.Window), _flip_order),
    Rule("bump-limit", "mutation", lambda n: isinstance(n, exp.Limit) and isinstance(n.expression, exp.Literal) and n.expression.name.isdigit(), _bump_limit),
    Rule("drop-negation", "mutation", lambda n: isinstance(n, exp.Not), _drop_not),
    Rule("drop-first-in-item", "mutation", lambda n: isinstance(n, exp.In) and len(n.expressions) >= 2 and not n.args.get("query"), _drop_in_item),
    Rule("negate-null-test", "mutation", lambda n: isinstance(n, exp.Is) and not isinstance(n.parent, exp.Not), _is_null_flip),
]


# --- applying rules -----------------------------------------------------------------------------------------------

def sites(tree: exp.Expression, rule: Rule) -> list[exp.Expression]:
    return [node for node in tree.walk() if rule.match(node)]


def apply_everywhere(tree: exp.Expression, rule: Rule) -> exp.Expression | None:
    """The tree with an equivalent rule applied at every site it matches (None when it matches none)."""
    if not sites(tree, rule):
        return None

    def visit(node: exp.Expression) -> exp.Expression:
        if rule.match(node):
            built = rule.build(node)
            return built if built is not None else node
        return node

    return tree.copy().transform(visit)


def apply_at(tree: exp.Expression, rule: Rule, index: int) -> exp.Expression | None:
    """The tree with a mutation applied at one site (the ``index``-th match in walk order)."""
    fresh = tree.copy()
    found = sites(fresh, rule)
    if index >= len(found):
        return None
    target = found[index]
    built = rule.build(target)
    if built is None:
        return None
    if target.parent is None:
        return built
    target.replace(built)
    return fresh


def to_sql(tree: exp.Expression) -> str:
    return tree.sql(dialect="bigquery")


@dataclass
class Variant:
    kind: str  # "equivalent" or "mutation"
    rule: str
    sql: str
    site: int | None = None  # mutation site index, None for rewrites
    sites_matched: int = 0


def variants(tree: exp.Expression, max_mutations: int = 8) -> list[Variant]:
    """Every rewrite (each rule at all its sites, plus all of them together) and a bounded set of mutations.

    Mutations are chosen to spread over the rules: the first site of each rule that matches,
    then second sites, until ``max_mutations``.
    """
    base = to_sql(tree)
    out: list[Variant] = []
    seen = {base}
    for rule in EQUIVALENT_RULES:
        built = apply_everywhere(tree, rule)
        if built is None:
            continue
        sql = to_sql(built)
        if sql in seen:
            continue
        seen.add(sql)
        out.append(Variant("equivalent", rule.name, sql, None, len(sites(tree, rule))))
    for name, fn in ROOT_REWRITES:
        built = fn(tree)
        if built is None:
            continue
        sql = to_sql(built)
        if sql not in seen:
            seen.add(sql)
            out.append(Variant("equivalent", name, sql))
    # all rule-based rewrites together
    combined = tree
    names = []
    for rule in EQUIVALENT_RULES:
        built = apply_everywhere(combined, rule)
        if built is not None:
            combined = built
            names.append(rule.name)
    if len(names) >= 2:
        sql = to_sql(combined)
        if sql not in seen:
            seen.add(sql)
            out.append(Variant("equivalent", "all-rewrites-together", sql, None, len(names)))
    # mutations, round robin over rules
    matched = {rule.name: len(sites(tree, rule)) for rule in MUTATIONS}
    picks: list[tuple[Rule, int]] = []
    depth = 0
    while len(picks) < max_mutations and depth < 4:
        added = False
        for rule in MUTATIONS:
            if matched[rule.name] > depth and len(picks) < max_mutations:
                picks.append((rule, depth))
                added = True
        if not added:
            break
        depth += 1
    for rule, depth in picks:
        count = matched[rule.name]
        index = 0 if depth == 0 else count - 1 if depth == 1 else count // 2
        built = apply_at(tree, rule, index)
        if built is None:
            continue
        sql = to_sql(built)
        if sql in seen:
            continue
        seen.add(sql)
        out.append(Variant("mutation", rule.name, sql, index, count))
    return out
