"""FLOAT64 rounding the SMT encoding must keep in comparisons and literals.

The SMT value sort is one exact rational sort, so two roundings were lost:

* A comparison of an INT64 operand with a FLOAT64 one converts the INT64 side first, which rounds past
  2**53: ``a = b AND b = c`` holds for ``a = 2**53``, ``b = 2**53``, ``c = 2**53 + 1`` although
  ``a = c`` does not. With declared types the INT64 side now reads as ``CAST(.. AS FLOAT64)``.
* A decimal or exponent literal outside the normal FLOAT64 range is not its exact value: ``1e-324`` and
  ``2e-324`` are both 0, ``1e400`` overflows. And an exponent literal is DOUBLE in another engine and MySQL even
  though their plain decimals are exact, so ``1e-1 + 2e-1`` is not ``3e-1`` there.

Each wrong pair returns different rows in another engine (whose DOUBLE and BIGINT round as FLOAT64 and INT64 do)
on the database next to it; the near misses were proven before the fix and stay proven.
"""

from collections import Counter

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import MIXED_NUMERIC_ASSUMPTION, prove_equivalent_smt

PROVERS = pytest.mark.parametrize("prove", [prove_equivalent_smt, prove_equivalent_algebraic], ids=["smt", "algebraic"])

BIG = 2**53  # 9007199254740992; 2**53 + 1 rounds to it as FLOAT64

MIXED_DDL = "CREATE TABLE mixed (a BIGINT, b DOUBLE, c BIGINT)"

MIXED_ROWS = [(BIG, float(BIG), BIG + 1), (BIG + 1, float(BIG), BIG)]

MIXED_SCHEMA = {"mixed": ["a", "b", "c"]}

MIXED_TYPES = {"mixed": {"a": "INT64", "b": "FLOAT64", "c": "INT64"}}

INT_TYPES = {"mixed": {"a": "INT64", "b": "INT64", "c": "INT64"}}

FLOAT_TYPES = {"mixed": {"a": "FLOAT64", "b": "FLOAT64", "c": "FLOAT64"}}

COMPARED_THROUGH_FLOAT = [
    pytest.param(
        "SELECT a FROM mixed WHERE a = b AND b = c", "SELECT a FROM mixed WHERE a = b AND b = c AND a = c", id="F5-transitivity"
    ),
    pytest.param("SELECT a AS v FROM mixed WHERE a = b", "SELECT b AS v FROM mixed WHERE a = b", id="equal-to-a-float-column"),
    pytest.param(
        "SELECT a FROM mixed WHERE a IN (b) AND c BETWEEN b AND b", "SELECT a FROM mixed WHERE a = c AND a IN (b)", id="in-and-between"
    ),
    pytest.param(
        "SELECT a FROM mixed WHERE CASE b WHEN a THEN TRUE END AND c = b",
        "SELECT a FROM mixed WHERE a = b AND c = a",
        id="simple-case-operand",
    ),
    pytest.param(
        "SELECT a FROM mixed m WHERE m.a IN (SELECT n.b FROM mixed n WHERE n.b = m.c)",
        "SELECT a FROM mixed m WHERE m.a IN (SELECT n.b FROM mixed n WHERE n.b = m.c) AND m.a = m.c",
        id="in-subquery",
    ),
]

@PROVERS
def test_undeclared_comparison_states_the_mixed_numeric_assumption(prove):
    # Without types a, b and c may all be INT64 (or all FLOAT64), where the pair is equivalent: proven, but
    # only under the stated assumption that compared values have the same numeric type.
    result = prove(*COMPARED_THROUGH_FLOAT[0].values, schema=MIXED_SCHEMA)
    assert result.proven
    assert MIXED_NUMERIC_ASSUMPTION in result.assumptions

BIG_FLOAT_LITERAL = (
    "SELECT a FROM mixed WHERE a = 1e16 AND c = 1e16",
    "SELECT a FROM mixed WHERE a = 1e16 AND c = 1e16 AND a = c",
)

LITERAL_DDL = "CREATE TABLE t (a BIGINT, x DOUBLE)"

LITERAL_ROWS = [(1, 0.5), (2, None)]

LITERAL_SCHEMA = {"t": ["a", "x"]}
