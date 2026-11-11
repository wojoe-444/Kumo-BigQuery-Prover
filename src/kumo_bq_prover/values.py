"""Decode BigQuery REST query results into comparable Python values.

The REST API returns every scalar as a string, a STRUCT as ``{"f": [{"v": ...}]}``
and an ARRAY as a list of ``{"v": ...}``. Values come back as hashable tuples so
a result set can be compared as a multiset.

Encoding of the decoded values:

* NULL -> ``None``
* INT64 -> ``int``; BOOL -> ``bool``; FLOAT64 -> ``float``
* NUMERIC / BIGNUMERIC -> ``decimal.Decimal``
* STRING, DATE, DATETIME, TIME, TIMESTAMP, JSON, GEOGRAPHY -> ``str`` as returned
  (TIMESTAMP is normalised to epoch microseconds as ``int``)
* BYTES -> ``bytes``
* ARRAY -> ``("ARRAY", (item, ...))``
* STRUCT -> ``("STRUCT", ((name, value), ...))``
"""

from __future__ import annotations

import base64
import decimal
import math
from collections import Counter
from typing import Any

_INT = {"INTEGER", "INT64"}
_FLOAT = {"FLOAT", "FLOAT64"}
_BOOL = {"BOOLEAN", "BOOL"}
_NUMERIC = {"NUMERIC", "BIGNUMERIC"}
_STRUCT = {"RECORD", "STRUCT"}


def decode_cell(value: Any, field: dict) -> Any:
    """Decode one cell of a REST response given its schema field."""
    if field.get("mode") == "REPEATED":
        items = value or []
        element = dict(field, mode="NULLABLE")
        return ("ARRAY", tuple(decode_cell(item.get("v"), element) for item in items))
    if value is None:
        return None
    kind = field["type"].upper()
    if kind in _STRUCT:
        subfields = field.get("fields", [])
        cells = value.get("f", [])
        return (
            "STRUCT",
            tuple((sub.get("name"), decode_cell(cell.get("v"), sub)) for sub, cell in zip(subfields, cells)),
        )
    if kind in _INT:
        return int(value)
    if kind in _BOOL:
        return str(value).lower() == "true"
    if kind in _FLOAT:
        return float(value)
    if kind in _NUMERIC:
        return decimal.Decimal(value)
    if kind == "BYTES":
        return base64.b64decode(value)
    if kind == "TIMESTAMP":
        # The REST API sends seconds since the epoch as a decimal string.
        return int((decimal.Decimal(value) * 1_000_000).to_integral_value())
    return value


def decode_rows(schema: dict, rows: list[dict] | None) -> list[tuple]:
    """Decode a REST ``rows`` array into a list of row tuples."""
    fields = schema.get("fields", [])
    decoded = []
    for row in rows or []:
        cells = row.get("f", [])
        decoded.append(tuple(decode_cell(cell.get("v"), field) for field, cell in zip(fields, cells)))
    return decoded


def _canonical(value: Any) -> Any:
    """Make a decoded value safe to hash and compare (NaN equals NaN, -0.0 equals 0.0)."""
    if isinstance(value, float):
        if math.isnan(value):
            return ("NAN",)
        return 0.0 if value == 0.0 else value
    if isinstance(value, tuple) and value and value[0] == "ARRAY":
        return ("ARRAY", tuple(_canonical(item) for item in value[1]))
    if isinstance(value, tuple) and value and value[0] == "STRUCT":
        return ("STRUCT", tuple((name, _canonical(item)) for name, item in value[1]))
    return value


def multiset(rows: list[tuple]) -> Counter:
    """Rows as an unordered multiset: column names are ignored, positions are not."""
    return Counter(tuple(_canonical(cell) for cell in row) for row in rows)


def same_rows(left: list[tuple], right: list[tuple], ordered: bool = False) -> bool:
    if ordered:
        return [tuple(_canonical(c) for c in r) for r in left] == [tuple(_canonical(c) for c in r) for r in right]
    return multiset(left) == multiset(right)
