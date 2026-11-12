"""Executed counterexample search behind ``prove_equivalent_algebraic(search_counterexample=True)``."""

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402

from kumo_bq_prover.ported.smt_equivalence import SmtStatus, TableConstraints  # noqa: E402

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}

TYPES = {name: {c: "INT64" for c in cols} for name, cols in SCHEMA.items()}

def test_off_by_default():
    left, right = "SELECT t.b AS p FROM t LEFT JOIN u ON t.a = u.a", "SELECT t.b AS p FROM t JOIN u ON t.a = u.a"
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False)
    assert result.status is SmtStatus.NOT_PROVEN
