"""Equivalence prover for BigQuery SQL only.

Nothing here translates BigQuery SQL into another dialect. Queries are parsed
with sqlglot's BigQuery dialect and executed, when execution is needed, on the
BigQuery emulator (see docs/emulator-decision.md).
"""

from .verdict import Verdict

__all__ = ["Verdict"]
