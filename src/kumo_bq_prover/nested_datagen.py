"""Seeded random databases with ARRAY and STRUCT columns.

``datagen.random_database`` handles flat tables. The inferred schemas of real
queries (GA4 ``event_params``, ``hits.product``) nest ARRAY and STRUCT, so this
module builds CREATE TABLE and INSERT statements for them too.

Values are drawn from small pools per "column class" (columns the query
compares for equality share one), so joins find matches and filters see the
constants the query mentions (``harvest.harvest``). The same seed gives the
same database.
"""

from __future__ import annotations

import datetime
import random

from .datagen import _POOLS, literal
from .schema_infer import ColType, type_sql

#: Chance a cell is NULL when the pool does not already contain one.
NULL_SHARE = 0.08


def flatten(schema: dict[str, dict[str, ColType]]) -> dict[str, dict[str, str]]:
    """``{table: {leaf name: scalar type}}`` for ``harvest.harvest`` (nested names are leaves)."""
    flat: dict[str, dict[str, str]] = {}

    def walk(table: str, name: str, kind: ColType) -> None:
        if isinstance(kind, str):
            flat[table][name] = kind
        elif kind[0] == "ARRAY":
            walk(table, name, kind[1])
        else:
            for field_name, field_type in kind[1]:
                walk(table, field_name, field_type)

    for table, columns in schema.items():
        flat[table] = {}
        for column, kind in columns.items():
            walk(table, column, kind)
    return flat


def create_statements(schema: dict[str, dict[str, ColType]]) -> list[str]:
    return [
        f"CREATE TABLE {table} ({', '.join(f'{name} {type_sql(kind)}' for name, kind in columns.items())})"
        for table, columns in schema.items()
    ]


_DATES = [datetime.datetime(2019, 12, 31, 8, 30), datetime.datetime(2020, 3, 31, 23, 59, 58), datetime.datetime(2021, 1, 15, 12, 0), datetime.datetime(2017, 8, 1, 0, 0, 1)]
_NUMBERS = {"int": ["0", "1", "2", "10", "25"], "float": ["0", "0.5", "1.5", "10.25", "2"]}


def _parses(flavour: str, text: str) -> bool:
    try:
        if flavour in ("int", "float") and text != text.strip():
            return False  # the emulator, unlike BigQuery, does not trim before it casts
        if flavour == "int":
            int(text)
        elif flavour == "float":
            float(text)
        else:
            datetime.datetime.strptime(text, flavour)
    except ValueError:
        return False
    return True


def flavour_pool(flavour: str) -> list[str]:
    """Strings that survive CAST or PARSE on the format: numbers, or dates rendered with the format."""
    if flavour in _NUMBERS:
        return list(_NUMBERS[flavour])
    return [d.strftime(flavour) for d in _DATES]


class _Generator:
    def __init__(self, seed: int, extra: dict[str, list], classes: dict[tuple, int], exact: dict[str, list], flavours: dict[tuple, str]) -> None:
        self.flavours = flavours
        self.rng = random.Random(seed)
        self.extra = extra
        self.exact = exact
        self.classes = classes
        self.pools: dict[tuple, list] = {}

    def pool(self, key: tuple, leaf: str, kind: str) -> list:
        pool_key = (self.classes.get(key, key), kind)
        flavour = self.flavours.get(key) if kind == "STRING" else None
        if flavour and pool_key not in self.pools:
            own = flavour_pool(flavour)
            keep = [v for v in self.exact.get(leaf, []) + self.extra.get(leaf, []) if isinstance(v, str) and _parses(flavour, v)]
            self.pools[pool_key] = self.rng.sample(own, k=min(len(own), 3)) + keep * 2
        if pool_key not in self.pools:
            base = [v for v in _POOLS[kind] if v is not None]
            harvested = [v for v in self.extra.get(leaf, []) if isinstance(v, type(base[0]))][:8] if base else []
            wanted = [v for v in self.exact.get(leaf, []) if isinstance(v, type(base[0]))] if base else []
            # the constants themselves count triple, so filters on them match some rows
            chosen = self.rng.sample(base, k=min(len(base), 4)) + harvested + wanted * 3
            self.pools[pool_key] = chosen
        return self.pools[pool_key]

    def scalar(self, key: tuple, leaf: str, kind: str) -> str:
        if kind not in _POOLS:
            kind = "STRING"
        if self.rng.random() < NULL_SHARE:
            return "NULL"
        return literal(kind, self.rng.choice(self.pool(key, leaf, kind)))

    def value(self, key: tuple, leaf: str, kind: ColType) -> str:
        if isinstance(kind, str):
            return self.scalar(key, leaf, kind)
        if kind[0] == "ARRAY":
            count = self.rng.choice([0, 1, 2, 2, 3, 4])
            items = ", ".join(self.value(key, leaf, kind[1]) for _ in range(count))
            return f"{type_sql(kind)}[{items}]"
        fields = ", ".join(
            f"{self.value(key + (name,), name, field_type)} AS {name}" for name, field_type in kind[1]
        )
        return f"STRUCT({fields})"


def random_database(
    schema: dict[str, dict[str, ColType]],
    seed: int,
    extra: dict[str, list] | None = None,
    classes: dict[tuple, int] | None = None,
    max_rows: int = 10,
    exact: dict[str, list] | None = None,
    flavours: dict[tuple, str] | None = None,
) -> list[str]:
    """Setup statements for one random database over a nested schema.

    ``extra`` maps a leaf column name to values worth trying; ``exact`` maps it to the
    constants themselves (``harvest(..., exact=True)``); ``classes`` maps
    ``(table, column, field, ...)`` to a class id so linked columns share values; ``flavours`` maps a
    STRING column to the format its strings must parse with (``Schema.flavours``).
    """
    gen = _Generator(seed, extra or {}, classes or {}, exact or {}, flavours or {})
    statements = create_statements(schema)
    for table, columns in schema.items():
        count = 0 if gen.rng.random() < 0.05 else gen.rng.randint(3, max_rows)
        if not count:
            continue
        rows = []
        for _ in range(count):
            cells = [gen.value((table, name), name, kind) for name, kind in columns.items()]
            rows.append("(" + ", ".join(cells) + ")")
        statements.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join(rows)}")
    return statements
