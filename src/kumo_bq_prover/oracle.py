"""The execution oracle: run two queries on the emulator and compare rows.

The oracle can only ever *refute*. Two queries that agree on the data it
built are UNKNOWN, never PROVEN. A query the emulator rejects makes the whole
comparison UNKNOWN (the emulator's limits are not BigQuery's behaviour).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .emulator import Client, EmulatorUnavailable, QueryError
from .values import same_rows
from .verdict import Verdict


@dataclass
class OracleResult:
    verdict: Verdict
    reason: str
    #: The setup statements the counterexample ran on (only when REFUTED).
    setup: list[str] = field(default_factory=list)
    left_rows: list[tuple] = field(default_factory=list)
    right_rows: list[tuple] = field(default_factory=list)


def compare(
    client: Client,
    left_sql: str,
    right_sql: str,
    setup: list[str] | None = None,
    ordered: bool = False,
) -> OracleResult:
    """Build the ``setup`` statements in a scratch dataset, run both queries, compare.

    ``setup`` is a list of GoogleSQL statements (CREATE TABLE ... AS SELECT,
    INSERT, ...). Table names in the queries resolve against the scratch dataset.
    """
    setup = list(setup or [])
    try:
        with client.scratch_dataset() as dataset:
            for statement in setup:
                client.query(statement, dataset)
            left = client.query(left_sql, dataset)
            right = client.query(right_sql, dataset)
    except QueryError as error:
        return OracleResult(Verdict.UNKNOWN, f"emulator rejected a statement: {error}")
    except EmulatorUnavailable as error:
        return OracleResult(Verdict.UNKNOWN, f"emulator unavailable: {error}")
    if same_rows(left.rows, right.rows, ordered=ordered):
        return OracleResult(Verdict.UNKNOWN, "agree on the built data (not a proof)")
    return OracleResult(
        Verdict.REFUTED,
        "different rows on the built data",
        setup=setup,
        left_rows=left.rows,
        right_rows=right.rows,
    )
