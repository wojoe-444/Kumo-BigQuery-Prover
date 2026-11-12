"""Seeded random databases for the emulator oracle.

Given a schema ``{table: {column: type}}`` this makes CREATE TABLE and INSERT
statements in GoogleSQL, with values picked to expose differences: NULLs,
zero, negative numbers, values past 2**53, empty and mixed-case strings.
The same seed always gives the same database.
"""

from __future__ import annotations

import random

_ALIASES = {
    "INTEGER": "INT64", "INT": "INT64", "INT64": "INT64", "BIGINT": "INT64",
    "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64", "DOUBLE": "FLOAT64",
    "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC", "BIGNUMERIC": "BIGNUMERIC",
    "VARCHAR": "STRING", "STRING": "STRING", "TEXT": "STRING",
    "BOOLEAN": "BOOL", "BOOL": "BOOL",
    "BYTES": "BYTES", "DATE": "DATE", "DATETIME": "DATETIME", "TIMESTAMP": "TIMESTAMP",
}

_POOLS: dict[str, list] = {
    "INT64": [0, 1, 2, 3, -1, 10, 100, 9007199254740993, None, None],
    "FLOAT64": [0.0, 0.5, 1.5, -1.0, 2.5, 0.1, None, None],
    "NUMERIC": ["0", "0.5", "1.25", "-1", "10.000000001", "0.1", None, None],
    "BIGNUMERIC": ["0", "0.5", "1.25", "-1", "0.1", None, None],
    "STRING": ["", "a", "A", "b", "ab", " a", "Ab", None, None],
    "BOOL": [True, False, None],
    "BYTES": ["a", "b", None],
    "DATE": ["2024-01-01", "2024-02-29", "2023-12-31", None],
    "DATETIME": ["2024-01-01 00:00:00", "2024-02-29 12:30:45", None],
    "TIMESTAMP": ["2024-01-01 00:00:00+00", "2024-02-29 12:30:45+00", None],
}


#: Values near the float limit. They are only drawn by databases made with ``extremes=True``: the emulator
#: disagrees with the GoogleSQL reference on such magnitudes (see ``trust.py``), so a refutation built on
#: them is reported UNKNOWN, and a database without them can still give a trusted counterexample.
EXTREME_POOLS: dict[str, list] = {"FLOAT64": [1e308]}

#: Extra edge values, off by default: infinities, values past 2**53 in a sum, and NUMERIC
#: values at the scale limit, the traps random data almost never reaches. GoogleSQL has no
#: literal for an infinity, so FLOAT64 specials are strings rendered as IEEE_DIVIDE.
#: Not offered, because the emulator cannot hold them: NaN reads back as NULL and -0.0 as
#: 0.0, and CAST('NaN' AS FLOAT64) / CAST('inf' AS FLOAT64) in an INSERT fail to format.
SPECIAL_POOLS: dict[str, list] = {
    "FLOAT64": ["inf", "-inf", 1e16, 0.045],
    "NUMERIC": ["0.000000001", "0.045", "-0.5"],
    "BIGNUMERIC": ["0.000000001", "0.045"],
}
_FLOAT_SPECIALS = {"inf": "IEEE_DIVIDE(1, 0)", "-inf": "IEEE_DIVIDE(0 - 1, 0)"}


def normalize_type(name: str) -> str:
    key = name.upper().split("(")[0].strip()
    if key not in _ALIASES:
        raise ValueError(f"unsupported column type: {name}")
    return _ALIASES[key]


def literal(kind: str, value) -> str:
    if value is None:
        return "NULL"
    if kind == "INT64":
        return str(value)
    if kind == "FLOAT64":
        if isinstance(value, str) and value in _FLOAT_SPECIALS:
            return _FLOAT_SPECIALS[value]
        return repr(float(value))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return f"{kind} '{value}'"
    if kind == "STRING":
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    if kind == "BOOL":
        return "TRUE" if value else "FALSE"
    if kind == "BYTES":
        return "b'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    return f"{kind} '{value}'"


def create_statements(schema: dict[str, dict[str, str]]) -> list[str]:
    return [
        f"CREATE TABLE {table} ({', '.join(f'{c} {normalize_type(t)}' for c, t in cols.items())})"
        for table, cols in schema.items()
    ]


def insert_statement(table: str, columns: dict[str, str], rows: list[list]) -> str:
    kinds = [normalize_type(t) for t in columns.values()]
    body = ", ".join("(" + ", ".join(literal(k, v) for k, v in zip(kinds, row)) + ")" for row in rows)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES {body}"


#: Chance that a cell with harvested values takes one of them instead of the base pool.
HARVESTED_SHARE = 0.6


def random_database(
    schema: dict[str, dict[str, str]],
    seed: int,
    max_rows: int = 4,
    extra: dict[str, list] | None = None,
    specials: bool = False,
    extremes: bool = False,
) -> list[str]:
    """Setup statements for one random database (tables, then their rows).

    ``extra`` maps a column name to values worth trying (see ``harvest.harvest``);
    those cells draw from them with probability ``HARVESTED_SHARE``. ``specials``
    adds ``SPECIAL_POOLS`` to the base pools and ``extremes`` adds ``EXTREME_POOLS``.
    """
    rng = random.Random(seed)
    extra = extra or {}
    statements = create_statements(schema)

    def cell(column: str, kind: str):
        if extra.get(column) and rng.random() < HARVESTED_SHARE:
            return rng.choice(extra[column])
        pool = _POOLS[kind] + (SPECIAL_POOLS.get(kind, []) if specials else []) + (EXTREME_POOLS.get(kind, []) if extremes else [])
        return rng.choice(pool)

    for table, cols in schema.items():
        kinds = [normalize_type(t) for t in cols.values()]
        rows = [[cell(c, k) for c, k in zip(cols, kinds)] for _ in range(rng.randint(0, max_rows))]
        if rows:
            statements.append(insert_statement(table, cols, rows))
    return statements


def fixed_database(schema: dict[str, dict[str, str]], table: str, columns: list[str], rows: list[list]) -> list[str]:
    """Setup statements holding exactly the given rows in one table (other tables empty)."""
    statements = create_statements(schema)
    if rows:
        cols = {c: schema[table][c] for c in columns}
        statements.append(insert_statement(table, cols, rows))
    return statements
