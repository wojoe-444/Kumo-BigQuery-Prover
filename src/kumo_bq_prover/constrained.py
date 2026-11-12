"""Databases that respect declared keys, for pairs that are equivalent only under constraints.

A pair like ``Track JOIN MediaType`` against ``Track`` is equivalent only because
``MediaType.MediaTypeId`` is a key and ``Track.MediaTypeId`` is a NOT NULL foreign key to it.
Random rows that break those declarations would "refute" a true equivalence, so the data
for these corpora is generated under the declarations read from the DDL (BigQuery DDL with
``PRIMARY KEY (...) NOT ENFORCED`` and ``FOREIGN KEY ... REFERENCES ... NOT ENFORCED``).

A pair can name declarations to ``drop`` (``pk:T``, ``fk:T.c``, ``not_null:T.c``): the
data then ignores them, which is how the "sibling" pairs that need a missing declaration
get their counterexamples. The emulator never sees the constraints, only the rows.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from .datagen import HARVESTED_SHARE, insert_statement, normalize_type

_TABLE = re.compile(r"CREATE TABLE\s+`?([\w ]+?)`?\s*\((.*?)\n\);", re.S | re.I)
_COLUMN = re.compile(r"^\s*(\w+)\s+([A-Z0-9]+(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?)(\s+NOT NULL)?\s*,?\s*$", re.I)
_PK = re.compile(r"PRIMARY KEY\s*\(([^)]*)\)", re.I)
_FK = re.compile(r"FOREIGN KEY\s*\(([^)]*)\)\s*REFERENCES\s+`?([\w ]+?)`?\s*\(([^)]*)\)", re.I)


@dataclass
class Table:
    name: str
    columns: dict[str, str] = field(default_factory=dict)  # name -> BigQuery type
    not_null: set[str] = field(default_factory=set)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[tuple[list[str], str, list[str]]] = field(default_factory=list)


def _names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_ddl(text: str) -> dict[str, Table]:
    """Read tables, NOT NULL columns, primary keys and foreign keys from BigQuery DDL."""
    tables: dict[str, Table] = {}
    for match in _TABLE.finditer(re.sub(r"--[^\n]*", "", text)):
        table = Table(match.group(1))
        for line in match.group(2).splitlines():
            if not line.strip():
                continue
            pk, fk = _PK.search(line), _FK.search(line)
            if pk:
                table.primary_key = _names(pk.group(1))
            elif fk:
                table.foreign_keys.append((_names(fk.group(1)), fk.group(2), _names(fk.group(3))))
            else:
                column = _COLUMN.match(line)
                if not column:
                    raise ValueError(f"cannot read DDL line of {table.name}: {line!r}")
                table.columns[column.group(1)] = column.group(2)
                if column.group(3):
                    table.not_null.add(column.group(1))
        tables[table.name] = table
    return tables


def _lookup(tables: dict[str, Table], name: str) -> Table:
    for key, table in tables.items():
        if key.lower() == name.lower():
            return table
    raise KeyError(name)


def referenced_tables(tables: dict[str, Table], *queries: str) -> list[str]:
    """Names (as declared) of the tables the queries read, in declaration order."""
    import sqlglot
    from sqlglot import exp

    seen = set()
    for sql in queries:
        for node in sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table):
            seen.add(node.name.lower())
    return [name for name in tables if name.lower() in seen]


@dataclass
class Drops:
    pk: set[str] = field(default_factory=set)
    fk: set[tuple[str, str]] = field(default_factory=set)
    not_null: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def parse(cls, items: list[str] | None) -> "Drops":
        drops = cls()
        for item in items or []:
            kind, _, target = item.partition(":")
            if kind == "pk":
                drops.pk.add(target.lower())
            else:
                table, _, column = target.partition(".")
                (drops.fk if kind == "fk" else drops.not_null).add((table.lower(), column.lower()))
        return drops


_VALUES = {
    "INT64": [0, 1, 2, 3, 10],
    "FLOAT64": [0.0, 1.5, -1.0],
    "NUMERIC": ["0", "1.25", "-1"],
    "BIGNUMERIC": ["0", "1.25"],
    "STRING": ["a", "b", "ab", "A", ""],
    "BOOL": [True, False],
    "DATE": ["2024-01-01", "2024-02-29"],
    "DATETIME": ["2024-01-01 00:00:00", "2024-02-29 12:30:45"],
    "TIMESTAMP": ["2024-01-01 00:00:00+00"],
    "BYTES": ["a", "b"],
}
NULL_RATE = 0.25


def _pool(kind: str) -> list:
    return _VALUES[normalize_type(kind)]


def _parents_first(tables: dict[str, Table], names: list[str]) -> list[str]:
    by_lower = {n.lower(): n for n in names}
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order or name in visiting:
            return
        visiting.add(name)
        for _columns, parent, _parent_columns in tables[name].foreign_keys:
            if parent.lower() in by_lower:
                visit(by_lower[parent.lower()])
        visiting.discard(name)
        order.append(name)

    for name in names:
        visit(name)
    return order


def _repair(tables: dict[str, Table], names: list[str], rows_of: dict[str, list[dict]], drops: Drops, rng: random.Random) -> None:
    """Make every declared foreign key hold.

    Rows are drawn parents first, which cannot work for a self-reference or a cycle of foreign
    keys (store and staff point at each other). A dangling value takes a real parent key, else
    NULL when the column is nullable, else its row is dropped; dropping can orphan other rows,
    so this repeats until nothing changes.
    """
    by_lower = {n.lower(): n for n in names}
    for _ in range(100):
        changed = False
        for name in names:
            table = tables[name]
            for columns, parent, parent_columns in table.foreign_keys:
                if parent.lower() not in by_lower or any((name.lower(), c.lower()) in drops.fk for c in columns):
                    continue
                parent_rows = rows_of[by_lower[parent.lower()]]
                keys = {tuple(r[c] for c in parent_columns) for r in parent_rows}
                nullable = all(c not in table.not_null or (name.lower(), c.lower()) in drops.not_null for c in columns)
                for row in list(rows_of[name]):
                    values = tuple(row[c] for c in columns)
                    if all(v is None for v in values) or values in keys:
                        continue
                    changed = True
                    if parent_rows and (not nullable or rng.random() < 0.5):
                        for c, v in zip(columns, tuple(rng.choice(parent_rows)[pc] for pc in parent_columns)):
                            row[c] = v
                    elif nullable:
                        for c in columns:
                            row[c] = None
                    else:
                        rows_of[name].remove(row)
        if not changed:
            return


def database(
    tables: dict[str, Table],
    names: list[str],
    seed: int,
    drops: Drops | None = None,
    max_rows: int = 4,
    extra: dict[str, list] | None = None,
) -> list[str]:
    """Setup statements for one random database over ``names`` that honours the declarations.

    ``extra`` maps a column name to values worth trying (see ``harvest.harvest``); a cell that
    is not a key takes one of them with probability ``HARVESTED_SHARE``.
    """
    drops = drops or Drops()
    rng = random.Random(seed)
    extra = extra or {}
    rows_of: dict[str, list[dict]] = {}
    for name in _parents_first(tables, names):  # a cycle falls back to a small domain for the late FK
        table = tables[name]
        rows: list[dict] = []
        key = [] if name.lower() in drops.pk else table.primary_key
        fk_of = {}
        for columns, parent, parent_columns in table.foreign_keys:
            if all((name.lower(), c.lower()) not in drops.fk for c in columns):
                for column in columns:
                    fk_of[column] = (columns, parent, parent_columns)
        for _ in range(rng.randint(0, max_rows)):
            for _attempt in range(20):
                row = {}
                done = set()
                for column, kind in table.columns.items():
                    if column in done:
                        continue
                    nullable = column not in table.not_null or (name.lower(), column.lower()) in drops.not_null
                    if column in fk_of:
                        columns, parent, parent_columns = fk_of[column]
                        parent_rows = rows_of.get(next((n for n in names if n.lower() == parent.lower()), ""), None)
                        if all(c in table.not_null and (name.lower(), c.lower()) not in drops.not_null for c in columns):
                            allow_null = False
                        else:
                            allow_null = True
                        if allow_null and rng.random() < NULL_RATE:
                            picked = [None] * len(columns)
                        elif parent_rows:
                            source = rng.choice(parent_rows)
                            picked = [source[c] for c in parent_columns]
                        elif parent_rows is not None:
                            picked = None  # the parent table is in the database but empty: no legal child row
                        else:
                            picked = [rng.choice(_pool(table.columns[c])) for c in columns]
                        if picked is None:
                            row = None
                            break
                        for c, v in zip(columns, picked):
                            row[c] = v
                            done.add(c)
                        continue
                    if nullable and rng.random() < NULL_RATE:
                        row[column] = None
                    elif extra.get(column) and rng.random() < HARVESTED_SHARE:
                        row[column] = rng.choice(extra[column])
                    else:
                        row[column] = rng.choice(_pool(kind))
                if row is None:
                    break
                if key and any(tuple(r[c] for c in key) == tuple(row[c] for c in key) for r in rows):
                    continue
                if key and any(row[c] is None for c in key):
                    continue
                rows.append(row)
                break
        rows_of[name] = rows
    _repair(tables, names, rows_of, drops, rng)
    statements = [
        f"CREATE TABLE `{n}` ({', '.join(f'{c} {_ddl_type(t)}' for c, t in tables[n].columns.items())})" for n in names
    ]
    for name in names:
        if rows_of[name]:
            columns = {c: _ddl_type(t) for c, t in tables[name].columns.items()}
            statements.append(insert_statement(f"`{name}`", columns, [[r[c] for c in columns] for r in rows_of[name]]))
    return statements


def _ddl_type(kind: str) -> str:
    return normalize_type(kind)


def witness_database(tables: dict[str, Table], names: list[str], witness: dict, drops: Drops | None = None) -> list[str]:
    """The pair's own small database: its rows, other columns completed with legal values.

    Foreign keys that are still declared get a parent row (all-default except the key) when
    the witness names none, so the completed database satisfies what it claims to.
    """
    drops = drops or Drops()
    by_lower = {n.lower(): n for n in tables}
    rows_of: dict[str, list[dict]] = {n: [] for n in tables}

    def default(table: Table, column: str):
        if column not in table.not_null:
            return None
        return _pool(table.columns[column])[0]

    def complete(table: Table, partial: dict) -> dict:
        lowered = {k.lower(): v for k, v in partial.items()}
        return {c: lowered[c.lower()] if c.lower() in lowered else default(table, c) for c in table.columns}

    for name, rows in witness.items():
        table = tables[by_lower[name.lower()]]
        for partial in rows:
            rows_of[table.name].append(complete(table, partial))

    changed = True
    guard = 0
    while changed and guard < 50:
        changed, guard = False, guard + 1
        for name, rows in list(rows_of.items()):
            table = tables[name]
            for row in list(rows):
                for columns, parent, parent_columns in table.foreign_keys:
                    if any((name.lower(), c.lower()) in drops.fk for c in columns):
                        continue
                    values = [row[c] for c in columns]
                    if any(v is None for v in values):
                        continue
                    parent_table = tables[by_lower[parent.lower()]]
                    if any(all(r[pc] == v for pc, v in zip(parent_columns, values)) for r in rows_of[parent_table.name]):
                        continue
                    partial = dict(zip(parent_columns, values))
                    rows_of[parent_table.name].append(complete(parent_table, partial))
                    changed = True
    created = [n for n in tables if n in names or rows_of[n]]
    statements = [
        f"CREATE TABLE `{n}` ({', '.join(f'{c} {_ddl_type(t)}' for c, t in tables[n].columns.items())})" for n in created
    ]
    for n in created:
        if rows_of[n]:
            columns = {c: _ddl_type(t) for c, t in tables[n].columns.items()}
            statements.append(insert_statement(f"`{n}`", columns, [[r[c] for c in columns] for r in rows_of[n]]))
    return statements
