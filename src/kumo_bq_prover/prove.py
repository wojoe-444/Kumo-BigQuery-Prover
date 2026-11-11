"""The public entry point: static proof first, then emulator refutation."""

from __future__ import annotations

from .emulator import Client
from .oracle import OracleResult, compare
from .static import prove_static
from .verdict import Verdict


def prove_equivalent(
    left_sql: str,
    right_sql: str,
    setup: list[str] | None = None,
    client: Client | None = None,
    ordered: bool = False,
) -> OracleResult:
    """PROVEN (static), REFUTED (emulator counterexample) or UNKNOWN.

    ``client`` is optional: with no emulator the answer is the static one.
    """
    if prove_static(left_sql, right_sql) is Verdict.PROVEN:
        return OracleResult(Verdict.PROVEN, "identical after parsing")
    if client is None:
        return OracleResult(Verdict.UNKNOWN, "no static proof and no emulator given")
    return compare(client, left_sql, right_sql, setup=setup, ordered=ordered)
