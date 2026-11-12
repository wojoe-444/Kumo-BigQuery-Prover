"""Static proof layers: no database is built, nothing is run.

Four layers, cheapest first. A layer that proves the pair ends the search; a layer that does not leaves the pair
to the next one.

1. ``identical``: both queries parse to the same tree (the original skeleton check).
2. ``structural``: ``ported.equivalence``, the conservative normaliser (CTE lifting and ordering, predicate and
   literal canonical forms), compared under bag semantics.
3. ``algebraic``: ``ported.algebraic_equivalence``, a rule-driven normaliser (join, aggregate, set-operation,
   window and NULL rules) followed by the SMT prover on the normal forms.
4. ``smt``: ``ported.smt_equivalence`` on the queries as written.

The layers are ported from Kumo_SQL_Prover with every other-engine path removed (docs/ported-layers.md). A proof is
bag equivalence of the result rows on every database the schema allows, on the databases where neither query raises
a runtime error. ``PROVEN`` here is stricter: a proof whose error report says the rewrite introduces a runtime
error, removes one the original has, or cannot be compared is ``UNKNOWN`` (with the reason in ``withheld``), because
"same rows" alone would hide a pair where one side raises. A proof lists its assumptions, which the caller must read.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .datagen import normalize_type
from .verdict import Verdict

LAYERS = ("identical", "structural", "algebraic", "smt")

#: Error reports that keep a proof: neither query has an operation that can fail (``none``), or each failure of one is
#: covered by a failure of the other (``same``). ``introduces`` (the rewrite can raise where the original succeeds),
#: ``refines`` (the original can raise where the rewrite cannot) and ``unknown`` (the layer could not compare them, which
#: happens after rewrites move the failing operation) withhold it: the rows are equal where neither query raises, and
#: nothing more is known.
_CLEAN_ERRORS = (None, "none", "same")


@dataclass(frozen=True)
class StaticResult:
    verdict: Verdict
    #: The layer that proved the pair, or the last layer tried.
    layer: str
    reason: str
    #: What the proof assumes (copied from the layer's result). Read these: "FLOAT64 values are never NaN" and
    #: "SUM and AVG are treated as independent of row order" are modelling choices, not facts about the data.
    assumptions: tuple[str, ...] = ()
    #: The layer's runtime-error report (``none`` or ``same`` on a proof), when it has one.
    errors: str | None = None
    #: Set when a layer proved the rows equal but the error report is not clean: the verdict is UNKNOWN, and this is
    #: the layer and error verdict that were withheld.
    withheld: str = ""


def solver_inputs(schema: dict[str, dict[str, str]] | None) -> dict:
    """``schema={table: [columns]}`` and ``types={table: {column: BigQuery type}}`` for the layers."""

    if not schema:
        return {}
    lowered = {table.lower(): {column.lower(): normalize_type(kind) for column, kind in columns.items()} for table, columns in schema.items()}
    return {"schema": {table: list(columns) for table, columns in lowered.items()}, "types": lowered}


def prove_static_detailed(
    left_sql: str,
    right_sql: str,
    schema: dict[str, dict[str, str]] | None = None,
    layers: tuple[str, ...] = LAYERS,
    ordered: bool = False,
) -> StaticResult:
    """Run the layers in order; the first proof wins.

    ``ordered=True`` asks for the same rows in the same order. The layers prove bags, so only an identical parse
    proves an ordered pair.
    """

    try:
        left = sqlglot.parse_one(left_sql, read="bigquery")
        right = sqlglot.parse_one(right_sql, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        return StaticResult(Verdict.UNKNOWN, "identical", f"parse error: {error}")
    if "identical" in layers and left == right:
        return StaticResult(Verdict.PROVEN, "identical", "identical after parsing")
    if ordered:
        return StaticResult(Verdict.UNKNOWN, "identical", "ordered comparison: only identical queries are proven")
    options = solver_inputs(schema)
    withheld = ""
    skipped = ""
    last = "identical"
    for layer in layers:
        if layer == "identical":
            continue
        last = layer
        if layer == "algebraic":
            exposure = float_aggregate_exposure([left, right], options.get("types", {}))
            if exposure:
                skipped = skipped or f"algebraic layer skipped: {exposure} feeds an aggregate that regrouping would reorder"
                continue
        outcome = _run_layer(layer, left_sql, right_sql, options)
        if outcome is None:
            continue
        status, reason, assumptions, error_verdict = outcome
        if not status:
            continue
        if layer == "algebraic" and error_verdict == "unknown" and cannot_fail(left) and cannot_fail(right):
            # The algebraic layer never compares errors (its normalization moves operations). With no operation that
            # can fail on either side there is nothing to compare.
            error_verdict = "none"
        if error_verdict not in _CLEAN_ERRORS:
            withheld = withheld or f"{layer}: rows equal, runtime errors {error_verdict}"
            continue
        return StaticResult(Verdict.PROVEN, layer, reason, assumptions, error_verdict)
    return StaticResult(Verdict.UNKNOWN, last, "no layer proved the pair" + (f" ({skipped})" if skipped else ""), withheld=withheld)


#: Syntax that cannot raise a runtime error. Everything not listed may (arithmetic can overflow, a division can hit zero,
#: a cast can fail, a scalar subquery can return two rows), so a pair that uses it is not called error-free here.
_NEVER_FAILS = (
    exp.Select, exp.Subquery, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Qualify, exp.Order, exp.Ordered,
    exp.Limit, exp.Offset, exp.Distinct, exp.With, exp.CTE, exp.Table, exp.TableAlias, exp.Alias, exp.Identifier,
    exp.Column, exp.Star, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.Dot, exp.Var, exp.Tuple, exp.DataType,
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.And, exp.Or, exp.Not, exp.Is,
    exp.In, exp.Between, exp.Like, exp.Exists, exp.Case, exp.If, exp.Coalesce, exp.Nullif, exp.Union, exp.Intersect,
    exp.Except, exp.Count, exp.Min, exp.Max, exp.Window, exp.WindowSpec, exp.RowNumber, exp.Rank, exp.DenseRank,
    exp.Lower, exp.Upper, exp.Concat, exp.DPipe, exp.Length, exp.Trim, exp.Boolean, exp.Lateral, exp.Unnest, exp.Array,
)


def cannot_fail(tree: exp.Expression) -> bool:
    """Whether every node of ``tree`` is syntax that cannot raise a runtime error (see ``_NEVER_FAILS``)."""

    for node in tree.walk():
        if not isinstance(node, _NEVER_FAILS):
            return False
        if isinstance(node, exp.Subquery) and not isinstance(node.parent, (exp.From, exp.Join, exp.CTE, exp.In, exp.Exists, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
            return False
        if isinstance(node, exp.Window) and not isinstance(node.this, (exp.RowNumber, exp.Rank, exp.DenseRank, exp.Count, exp.Min, exp.Max)):
            return False
    return True


_FLOAT_TYPES = {"FLOAT64"}
#: Aggregates whose result depends on the order and grouping of the additions, or that return FLOAT64 from any input.
_ALWAYS_FLOAT_AGGREGATES = (
    exp.Avg, exp.Stddev, exp.StddevPop, exp.StddevSamp, exp.Variance, exp.VariancePop, exp.Corr, exp.CovarPop, exp.CovarSamp,
)
#: Functions and operators that return FLOAT64 from integer input.
_FLOAT_PRODUCERS = (exp.Div, exp.Sqrt, exp.Exp, exp.Ln, exp.Log, exp.Pow, exp.Round, exp.Floor, exp.Ceil)


def float_aggregate_exposure(trees: list[exp.Expression], types: dict[str, dict[str, str]]) -> str | None:
    """Why the pair sums or averages something that may be FLOAT64, or ``None`` when every aggregate input is known not to be.

    FLOAT64 addition is not associative, so regrouping partial sums, which the algebraic layer does as if the numbers
    were reals, changes the result (`1e16 + 1 + 1` against `(1e16) + (1 + 1)`). The layer cannot tell, so a pair
    with a possible FLOAT64 aggregate input is not offered to it. A column of unknown type counts as possibly FLOAT64;
    an alias is followed to the expression it names.
    """

    by_name: dict[str, set[str]] = {}
    for columns in types.values():
        for name, kind in columns.items():
            by_name.setdefault(name, set()).add(kind)
    aliases: dict[str, list[exp.Expression]] = {}
    for tree in trees:
        for alias in tree.find_all(exp.Alias):
            aliases.setdefault(alias.alias.lower(), []).append(alias.this)

    def exposed(node: exp.Expression, seen: set[str]) -> str | None:
        for inner in node.walk():
            if isinstance(inner, _ALWAYS_FLOAT_AGGREGATES) or isinstance(inner, _FLOAT_PRODUCERS):
                return type(inner).__name__.upper()
            if isinstance(inner, exp.Literal) and not inner.is_string and not inner.name.lstrip("-").isdigit():
                return "a decimal literal"
            if isinstance(inner, (exp.Cast, exp.TryCast)):
                target = inner.args.get("to")
                if target is not None and target.sql("bigquery").upper().split("(")[0] in {"FLOAT64", "FLOAT", "DOUBLE", "NUMERIC", "BIGNUMERIC"} and target.sql("bigquery").upper().startswith(("FLOAT", "DOUBLE")):
                    return "a cast to FLOAT64"
            if isinstance(inner, exp.Column):
                name = inner.name.lower()
                known = by_name.get(name)
                if known is not None:
                    if known & _FLOAT_TYPES:
                        return f"column {name} is FLOAT64"
                    continue
                if name in aliases and name not in seen:
                    for definition in aliases[name]:
                        found = exposed(definition, seen | {name})
                        if found:
                            return found
                    continue
                return f"column {name} has no declared type"
        return None

    for tree in trees:
        for aggregate in tree.find_all(exp.AggFunc):
            if isinstance(aggregate, _ALWAYS_FLOAT_AGGREGATES):
                return type(aggregate).__name__.upper()
            if isinstance(aggregate, (exp.Sum,)):
                found = exposed(aggregate.this, set())
                if found:
                    return found
    return None


def _run_layer(layer: str, left_sql: str, right_sql: str, options: dict):
    """``(proved, reason, assumptions, error verdict)`` of one layer, or ``None`` when it raised (a layer that raises proves nothing)."""

    from .ported import algebraic_equivalence, equivalence, smt_equivalence

    try:
        if layer == "structural":
            result = equivalence.prove_equivalent(left_sql, right_sql)
            return result.status is equivalence.EquivalenceStatus.PROVEN_EQUIVALENT, result.reason, tuple(getattr(result, "assumptions", ()) or ()), None
        prover = algebraic_equivalence.prove_equivalent_algebraic if layer == "algebraic" else smt_equivalence.prove_equivalent_smt
        result = prover(left_sql, right_sql, dialect="bigquery", **options)
        errors = result.errors.verdict if result.errors is not None else None
        return result.proven, result.reason, tuple(result.assumptions), errors
    except Exception:  # noqa: BLE001 - a prover that crashes on a query has not proved it
        return None


def prove_static(left_sql: str, right_sql: str, schema: dict[str, dict[str, str]] | None = None) -> Verdict:
    """PROVEN when a static layer proves the pair, otherwise UNKNOWN."""

    return prove_static_detailed(left_sql, right_sql, schema).verdict
