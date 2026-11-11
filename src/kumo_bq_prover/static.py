"""Static layer. Placeholder: identical parsed queries are equivalent.

Everything is parsed with sqlglot's BigQuery dialect. The structural,
algebraic and SMT layers of Kumo_SQL_Prover are ported here module by module
(see docs/roadmap.md); each port drops its other-engine execution paths and lets the
emulator do that job.
"""

from __future__ import annotations

import sqlglot

from .verdict import Verdict


def prove_static(left_sql: str, right_sql: str) -> Verdict:
    """PROVEN only when both parse to the same tree; otherwise UNKNOWN."""
    try:
        left = sqlglot.parse_one(left_sql, read="bigquery")
        right = sqlglot.parse_one(right_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return Verdict.UNKNOWN
    return Verdict.PROVEN if left == right else Verdict.UNKNOWN
