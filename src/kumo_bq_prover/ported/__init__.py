"""The static proof layers, ported from Kumo_SQL_Prover with every other-engine path removed.

Importing the package first teaches sqlglot's BigQuery parser the syntax it lacks (``bigquery_syntax``), as every
parse the prover makes relies on it. Modules keep the names they have in Kumo_SQL_Prover so a fix can be carried
across. See docs/ported-layers.md for what was kept, what was cut and how each layer is measured.
"""

from . import bigquery_syntax as _bigquery_syntax  # noqa: F401

from .lift_subqueries import LiftDiagnostic, LiftResult, count_inline_subqueries, lift_subqueries
from .equivalence import EquivalenceResult, EquivalenceStatus, build_bag_verifier_sql, prove_equivalent
from .smt_equivalence import Counterexample, SmtEquivalenceResult, SmtStatus, prove_equivalent_smt

__all__ = [
    "LiftDiagnostic",
    "LiftResult",
    "count_inline_subqueries",
    "lift_subqueries",
    "EquivalenceResult",
    "EquivalenceStatus",
    "build_bag_verifier_sql",
    "prove_equivalent",
    "Counterexample",
    "SmtEquivalenceResult",
    "SmtStatus",
    "prove_equivalent_smt",
]
