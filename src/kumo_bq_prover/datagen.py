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
    "DATE": "DATE", "DATETIME": "DATETIME", "TIMESTAMP": "TIMESTAMP",
}

_POOLS: dict[str, list] = {
    "INT64": [0, 1, 2, 3, -1, 10, 100, 9007199254740993, None, None],
    "FLOAT64": [0.0, 0.5, 1.5, -1.0, 2.5, 0.1, 1e308, None, None],
    "NUMERIC": ["0", "0.5", "1.25", "-1", "10.000000001", "0.1", None, None],
    "BIGNUMERIC": ["0", "0.5", "1.25", "-1", "0.1", None, None],
    "STRING": ["", "a", "A", "b", "ab", " a", "Ab", None, None],
    "BOOL": [True, False, None],
    "DATE": ["2024-01-01", "2024-02-29", "2023-12-31", None],
    "DATETIME": ["2024-01-01 00:00:00", "2024-02-29 12:30:45", None],
    "TIMESTAMP": ["2024-01-01 00:00:00+00", "2024-02-29 12:30:45+00", None],
}


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
        return repr(float(value))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return f"{kind} '{value}'"
    if kind == "STRING":
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
    if kind == "BOOL":
        return "TRUE" if value else "FALSE"
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


def random_database(schema: dict[str, dict[str, str]], seed: int, max_rows: int = 4) -> list[str]:
    """Setup statements for one random database (tables, then their rows)."""
    rng = random.Random(seed)
    statements = create_statements(schema)
    for table, cols in schema.items():
        kinds = [normalize_type(t) for t in cols.values()]
        rows = [[rng.choice(_POOLS[k]) for k in kinds] for _ in range(rng.randint(0, max_rows))]
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
