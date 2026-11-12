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

Two refinements keep the guard from withholding differences the emulator got right:

* **Conditional constructs.** An entry of ``untrusted_constructs.json`` with a ``when`` field is untrusted only when
  the witness holds that kind of value: ``like:plain`` (LIKE with a literal pattern) only with a newline in the data,
  ``fn:SUBSTRING`` and string-position functions only with non-ASCII text (the emulator counts bytes there).
* **Exact rewrites** (:func:`adjudicate`). A difference that involves only ``IN``, ``BETWEEN`` or ``IS TRUE`` is
  checked again on queries written without them (``gap_rewrites.py``). If the difference survives, it is a
  counterexample for BigQuery whatever the emulator does with ``IN``; if not, the emulator's gap produced it.
"""

from __future__ import annotations

import decimal
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .constructs import UNPARSED, constructs
from .gap_rewrites import REWRITABLE, rewrite

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


def witness_facts(texts: Iterable[str] = (), rows: Iterable[Iterable[tuple]] = ()) -> set[str]:
    """The kinds of value the witness holds that make a conditional construct untrusted (``non-ascii``, ``newline``)."""
    facts: set[str] = set()
    strings = list(texts)

    def visit(cell: Any) -> None:
        if isinstance(cell, str):
            strings.append(cell)
        elif isinstance(cell, tuple) and cell and cell[0] in ("ARRAY", "STRUCT"):
            for item in cell[1]:
                visit(item[1] if cell[0] == "STRUCT" else item)

    for result in rows:
        for row in result:
            for cell in row:
                visit(cell)
    for text in strings:
        if any(ord(ch) > 127 for ch in text) or re.search(r"\\[ux][0-9a-fA-F]", text):
            facts.add("non-ascii")
        if "\n" in text or "\\n" in text or "\r" in text:
            facts.add("newline")
    return facts


CAST_TAGS = frozenset({"cast:INT64", "safe_cast:INT64"})


def _whole_numbers_beyond(texts: Iterable[str], rows: Iterable[Iterable[tuple]], limit: int) -> bool:
    for text in texts:
        for match in _NUMBER.finditer(text):
            token = match.group(0)
            if not re.search(r"[.eE]", token) and abs(int(token)) >= limit:
                return True
    stack: list[Any] = [cell for result in rows for row in result for cell in row]
    while stack:
        cell = stack.pop()
        if isinstance(cell, bool):
            continue
        if isinstance(cell, int) and abs(cell) >= limit:
            return True
        if isinstance(cell, tuple) and cell and cell[0] in ("ARRAY", "STRUCT"):
            stack.extend(item[1] if cell[0] == "STRUCT" else item for item in cell[1])
    return False


def construct_hits(*queries: str, facts: set[str] | None = None) -> list[str]:
    """The untrusted constructs the queries use, sorted. A query that does not parse is a hit of its own.

    A conditional construct (``when`` in the file) counts only when its condition is among ``facts``; with ``facts=None``
    every conditional construct counts (the cautious reading, for callers that have no witness).
    """
    untrusted = untrusted_constructs()
    hits: set[str] = set()
    for sql in queries:
        found = constructs(sql) & (untrusted.keys() | {UNPARSED})
        hits |= {tag for tag in found if facts is None or tag == UNPARSED or untrusted[tag].get("when") in (None, *facts)}
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
    setup = list(setup)
    left_rows, right_rows = list(left_rows), list(right_rows)
    facts = witness_facts(setup + [left_sql, right_sql], [left_rows, right_rows])
    return construct_hits(left_sql, right_sql, facts=facts) + value_hits(setup, [left_sql, right_sql], [left_rows, right_rows])


@dataclass
class Adjudication:
    """What to say about a difference the emulator found: report it (``trusted``) or withhold it, and with which rows."""

    trusted: bool
    reasons: list[str] = field(default_factory=list)
    left_rows: list[tuple] = field(default_factory=list)
    right_rows: list[tuple] = field(default_factory=list)
    #: True when the rows come from the rewritten queries (the emulator's own rows for the originals were wrong or unchecked).
    rewritten: bool = False


def adjudicate(
    run: Callable[[str], list[tuple]],
    left_sql: str,
    right_sql: str,
    setup: Iterable[str] = (),
    left_rows: Iterable[tuple] = (),
    right_rows: Iterable[tuple] = (),
) -> Adjudication:
    """Decide whether a difference on ``setup`` is a counterexample for BigQuery.

    ``run(sql)`` executes one query on the data of the witness and returns its rows (it may raise ``QueryError``).
    When the only untrusted constructs are ``IN``, ``BETWEEN`` and ``IS TRUE`` (:data:`REWRITABLE`), both queries are
    rewritten without them (``gap_rewrites.rewrite``) and run again: the difference counts if the rewritten pair still
    differs and the rewritten pair passes the ordinary checks. The returned rows are then the rewritten queries' rows.
    """
    from .emulator import QueryError

    setup, left_rows, right_rows = list(setup), list(left_rows), list(right_rows)
    reasons = untrusted_reasons(left_sql, right_sql, setup, left_rows, right_rows)
    if not reasons:
        return Adjudication(True, [], left_rows, right_rows)
    if not set(reasons) <= REWRITABLE:
        return Adjudication(False, reasons, left_rows, right_rows)
    if reasons and set(reasons) & CAST_TAGS and _whole_numbers_beyond(setup + [left_sql, right_sql], [left_rows, right_rows], 2**53):
        # ROUND turns an INT64 into a FLOAT64, which cannot hold integers from 2^53 exactly.
        return Adjudication(False, reasons + ["value:int-above-2^53"], left_rows, right_rows)
    new_left, new_right = rewrite(left_sql), rewrite(right_sql)
    if new_left is None or new_right is None:
        return Adjudication(False, reasons, left_rows, right_rows)
    try:
        rewritten_left, rewritten_right = run(new_left), run(new_right)
    except QueryError:
        return Adjudication(False, reasons, left_rows, right_rows)
    from .values import same_rows

    if same_rows(rewritten_left, rewritten_right):
        # The rewritten pair agrees: the difference came from the emulator's IN, BETWEEN or IS TRUE.
        return Adjudication(False, [f"{reason}: the difference disappears when written without it" for reason in reasons], rewritten_left, rewritten_right, True)
    again = untrusted_reasons(new_left, new_right, setup, rewritten_left, rewritten_right)
    if again:
        return Adjudication(False, again, rewritten_left, rewritten_right, True)
    return Adjudication(True, [], rewritten_left, rewritten_right, True)
