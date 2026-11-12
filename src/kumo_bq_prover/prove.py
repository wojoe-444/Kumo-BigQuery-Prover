"""The public entry point: static proof first, then emulator refutation."""

from __future__ import annotations

from .emulator import Client
from .oracle import OracleResult, compare
from .static import prove_static_detailed
from .verdict import Verdict


def prove_equivalent(
    left_sql: str,
    right_sql: str,
    setup: list[str] | None = None,
    client: Client | None = None,
    ordered: bool = False,
    schema: dict[str, dict[str, str]] | None = None,
) -> OracleResult:
    """PROVEN (a static layer), REFUTED (emulator counterexample) or UNKNOWN.

    ``schema`` is ``{table: {column: type}}``; the static layers use it for column types. ``client`` is optional:
    with no emulator the answer is the static one.
    """
    static = prove_static_detailed(left_sql, right_sql, schema, ordered=ordered)
    if static.verdict is Verdict.PROVEN:
        return OracleResult(Verdict.PROVEN, f"{static.layer}: {static.reason}", assumptions=static.assumptions)
    if client is None:
        return OracleResult(Verdict.UNKNOWN, "no static proof and no emulator given" + (f" ({static.withheld})" if static.withheld else ""))
    return compare(client, left_sql, right_sql, setup=setup, ordered=ordered)
