"""Where the emulator can be trusted, and where a refutation must become UNKNOWN.

The oracle refutes a pair when the emulator returns different rows. That is only a counterexample for
BigQuery if the emulator computes both answers the way BigQuery does. The GoogleSQL compliance run
(``docs/emulator-gaps.md``, ``tools/run_compliance.py``) measured where it does not. Two checks come out of it:

* **Constructs** (``untrusted_constructs.json``, written by ``tools/compliance_gaps.py``): a function,
  operator, cast or join shape that disagrees with Google's reference results in a large share of its
  compliance cases, or that is a known divergence. A pair using one is not refuted.
* **Values** (:func:`value_hits`): the compliance cases the emulator gets wrong concentrate on extreme
  or special values (NaN, infinities, signed zero, magnitudes near the float and INT64 limits,
  NUMERIC beyond nine decimals). A counterexample built from or returning such values is not trusted.

A refutation that fails either check is reported as UNKNOWN with the reason, never as REFUTED. This only
ever costs refutations: it cannot turn an unknown into a proof.
"""

from __future__ import annotations

import decimal
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .constructs import UNPARSED, constructs

DATA = Path(__file__).with_name("untrusted_constructs.json")
LARGE = 1e18  # float and NUMERIC magnitudes from here on are not trusted (the compliance cases with such values fail four times as often; none sit between 1e15 and 1e18)
LARGE_INT = 2**62  # integer magnitudes from here on are not trusted (INT64 sums overflow near the limit)
MAX_DECIMALS = 9  # BigQuery NUMERIC has nine; the emulator's wider values do not round the same way

_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_NUMERIC_LITERAL = re.compile(r"(?i)\b(?:big)?numeric\s*'\s*([-+]?[\d.]+)")
_SPECIAL = re.compile(r"(?i)\b(nan|inf|infinity|ieee_divide|is_nan|is_inf)\b|[-+]?\b1e30[0-9]\b")
_NEGATIVE_ZERO = re.compile(r"(?<![\w.])-\s*0\.0*(?![\d])")


@lru_cache(maxsize=1)
def untrusted_constructs() -> dict[str, dict]:
    """``{tag: evidence}`` from ``untrusted_constructs.json`` (empty when the file is missing)."""
    if not DATA.exists():
        return {}
    return json.loads(DATA.read_text())["untrusted"]


def construct_hits(*queries: str) -> list[str]:
    """The untrusted constructs the queries use, sorted. A query that does not parse is a hit of its own."""
    untrusted = untrusted_constructs()
    hits: set[str] = set()
    for sql in queries:
        hits |= constructs(sql) & (untrusted.keys() | {UNPARSED})
    return sorted(hits)


def _text_hits(text: str) -> set[str]:
    hits: set[str] = set()
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        try:
            value = float(token)
        except ValueError:
            continue
        is_integer = not re.search(r"[.eE]", token)
        if math.isinf(value) or abs(value) >= (LARGE_INT if is_integer else LARGE):
            hits.add("value:large-number")
    for literal in _NUMERIC_LITERAL.finditer(text):  # only NUMERIC and BIGNUMERIC literals have a scale limit
        fraction = literal.group(1).split(".", 1)[1] if "." in literal.group(1) else ""
        if len(fraction) > MAX_DECIMALS:
            hits.add("value:wide-decimal")
    if _SPECIAL.search(text):
        hits.add("value:nan-or-infinity")
    if _NEGATIVE_ZERO.search(text):
        hits.add("value:negative-zero")
    return hits


def _cell_hits(cell: Any) -> set[str]:
    hits: set[str] = set()
    if isinstance(cell, float):
        if math.isnan(cell) or math.isinf(cell):
            hits.add("value:nan-or-infinity")
        elif abs(cell) >= LARGE:
            hits.add("value:large-number")
        elif cell == 0.0 and math.copysign(1, cell) < 0:
            hits.add("value:negative-zero")
    elif isinstance(cell, bool):
        pass
    elif isinstance(cell, int):  # INT64 (a TIMESTAMP is epoch microseconds, far below the integer limit)
        if abs(cell) >= LARGE_INT:
            hits.add("value:large-number")
    elif isinstance(cell, decimal.Decimal):
        if cell.is_nan() or cell.is_infinite() or abs(cell) >= LARGE:
            hits.add("value:large-number")
        elif -cell.as_tuple().exponent > MAX_DECIMALS:
            hits.add("value:wide-decimal")
    elif isinstance(cell, tuple) and cell and cell[0] in ("ARRAY", "STRUCT"):
        items = cell[1]
        for item in items:
            hits |= _cell_hits(item[1] if cell[0] == "STRUCT" else item)
    return hits


def value_hits(setup: Iterable[str] = (), queries: Iterable[str] = (), rows: Iterable[Iterable[tuple]] = ()) -> list[str]:
    """Special or extreme values in the setup statements, the queries and the rows they returned."""
    hits: set[str] = set()
    for text in list(setup) + list(queries):
        hits |= _text_hits(text)
    for result in rows:
        for row in result:
            for cell in row:
                hits |= _cell_hits(cell)
    return sorted(hits)


def untrusted_reasons(
    left_sql: str,
    right_sql: str,
    setup: Iterable[str] = (),
    left_rows: Iterable[tuple] = (),
    right_rows: Iterable[tuple] = (),
) -> list[str]:
    """Why a refutation on this pair and data cannot be trusted (empty when it can)."""
    return construct_hits(left_sql, right_sql) + value_hits(setup, [left_sql, right_sql], [left_rows, right_rows])
