"""Run Google's GoogleSQL compliance cases on the BigQuery emulator.

The fixture (``benchmarks/googlesql_conformance``) holds every case of the ``.test`` files of
google/googlesql at a pinned commit: a query, the tables it reads (as ``CREATE TABLE ... AS SELECT``
blocks with their printed rows) and the result Google's reference implementation returns. The claimed
subset is the 4,748 queries that use only features BigQuery has.

For every claimed case the emulator runs the query and the answer is compared with the expected one:

========================  ==============================================================================
pass                      the same column types and rows (or both sides raise an error)
wrong                     a result with other rows or other column types: an emulator divergence
missing_error             the reference raises an error and the emulator returns rows
emulator_error            the reference returns rows and the emulator rejects or fails the query
fixture                   a table could not be built on the emulator (or does not read back as printed)
unscored                  the expected text has sub-microsecond digits or cannot be read
zone                      the expected values were computed in a default time zone BigQuery does not use
crash                     the emulator stopped answering during the case (the case is the suspect)
========================  ==============================================================================

Only ``wrong`` and ``missing_error`` can make the oracle refute a pair for a reason BigQuery does not
share; ``emulator_error`` is already UNKNOWN in the oracle. The runner never touches real BigQuery.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .emulator import Client, EmulatorUnavailable, QueryError
from .gsql_expected import (
    Expected,
    ExpectedError,
    Interval,
    NanoPrecision,
    UnorderedList,
    parse_expected,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "benchmarks" / "googlesql_conformance"
SOURCE = "google/googlesql @ d82db99 (googlesql/compliance/testdata/*.test, Apache-2.0)"
DEFAULT_TIME_ZONE = "America/Los_Angeles"

# --- the claimed subset ---------------------------------------------------------------------------
# The definition KumoSQL's GoogleSQL conformance eval fixed before its evaluator existed (see
# docs/evals/googlesql-conformance.md there): queries that use only features BigQuery has.

CLAIMED_FEATURES = frozenset(
    """
    ANALYTIC_FUNCTIONS NUMERIC_TYPE BIGNUMERIC_TYPE CIVIL_TIME INTERVAL_TYPE HAVING_IN_AGGREGATE
    SAFE_FUNCTION_CALL ORDER_BY_IN_AGGREGATE LIMIT_IN_AGGREGATE NULL_HANDLING_MODIFIER_IN_AGGREGATE
    NULL_HANDLING_MODIFIER_IN_ANALYTIC NULLS_FIRST_LAST_IN_ORDER_BY WITH_RECURSIVE WITH_ON_SUBQUERY
    GROUP_BY_STRUCT GROUP_BY_ARRAY GROUPING_SETS GROUP_BY_ROLLUP GROUPING_BUILTIN GROUP_BY_ALL
    MULTI_GROUPING_SETS QUALIFY PIVOT UNPIVOT BY_NAME CORRESPONDING CORRESPONDING_FULL
    LIKE_ANY_SOME_ALL LIKE_ANY_SOME_ALL_ARRAY ENFORCE_CONDITIONAL_EVALUATION IS_DISTINCT
    """.split()
)
EXCLUDED_FILES = re.compile(
    r"^(dml_|graph_|json_|proto|match_recognize|pipe_|anonymization|differential_privacy|aggregation_threshold|kll_|"
    r"analytic_kll|approx_|analytic_approx|analytic_hll|hll_|map_|vector|uuid|new_uuid|generate_uuid|rand$|tablesample|"
    r"range|orderby_range|compression|keys|aead|typeof|call_sql_|hop_tvf|tumble_tvf|align_operator|pipe_align|"
    r"authorization_|collation|orderby_collate|elementwise_|apply_lambda|array_functions_with_lambda|filter_fields|"
    r"replace_fields|multi_level_aggregation|group_rows|pico_timestamp|nano_timestamp|unnest_multiway|lateral_join|"
    r"enum_|cast_function_to_json|ai_functions|measure|top_level_table_statement|generalized_statement|invoke_view|"
    r"hints|no_tests)"
)
GOOGLESQL_ONLY_TYPES = re.compile(
    r"(?i)\b(int32|uint32|uint64|float32|float|proto|enum|new|map\s*<|range\s*<|uuid|graph_table|graph|json|"
    r"googlesql_test|kitchensinkpb|tokenlist|vector)\b"
)
QUERY_START = re.compile(r"(?is)^\s*(\(\s*)*(select|with)\b")
SPLIT_SALT = "googlesql-conformance:"


def split_of(stem: str) -> str:
    """``dev`` or ``heldout``: a quarter of the files, by a hash of the file name (KumoSQL's split)."""
    digest = int(hashlib.sha256((SPLIT_SALT + stem).encode()).hexdigest(), 16)
    return "heldout" if digest % 4 == 0 else "dev"


def _features(options: dict, key: str) -> set[str]:
    return {x.strip() for x in options.get(key, "").split(",") if x.strip()}


def claim(file: str, sql: str, options: dict) -> tuple[bool, str]:
    if EXCLUDED_FILES.match(file):
        return False, "GoogleSQL-only topic"
    if not QUERY_START.match(sql):
        return False, "not a query"
    missing = _features(options, "required_features") - CLAIMED_FEATURES
    if missing:
        return False, "feature " + ",".join(sorted(missing))
    if _features(options, "forbidden_features") & CLAIMED_FEATURES:
        return False, "result holds only without a claimed feature"
    if GOOGLESQL_ONLY_TYPES.search(sql) or GOOGLESQL_ONLY_TYPES.search(options.get("parameters", "")):
        return False, "GoogleSQL-only type"
    if not options.get("name"):
        return False, "unnamed"
    return True, ""


@dataclass
class Case:
    file: str
    name: str
    sql: str
    options: dict
    expected: str
    prepare: list[dict]
    claimed: bool
    reason: str = ""
    split: str = "dev"


def load_files(split: str) -> list[dict]:
    with gzip.open(FIXTURE / f"{split}.json.gz", "rt", encoding="utf-8") as handle:
        return json.load(handle)["files"]


def cases(split: str = "dev") -> list[Case]:
    out = []
    for entry in load_files(split):
        carried = dict(entry["defaults"])
        for block in entry["prepare"]:
            if "default_time_zone" in block["options"]:
                carried["default_time_zone"] = block["options"]["default_time_zone"]
        for case in entry["cases"]:
            for key, value in case["options"].items():
                if key.startswith("default ") and key != "default global_labels":
                    carried[key[len("default "):].strip()] = value
            options = {**carried, **case["options"]}
            if "required_features" in carried and "required_features" not in case["options"]:
                options["required_features"] = carried["required_features"]
            expected = case["expected"][0] if case["expected"] else ""
            claimed, reason = claim(entry["file"], case["sql"], options)
            out.append(Case(entry["file"], case["name"], case["sql"], options, expected, entry["prepare"], claimed, reason, split))
    return out


# --- canonical values -----------------------------------------------------------------------------
# Both the expected rows (from gsql_expected) and the emulator's REST cells are turned into the same
# plain values before they are compared.

_NAN = "NaN"
_TIME_TEXT = re.compile(r"^(\d\d?):(\d\d?):(\d\d?)(?:\.(\d+))?$")
_INTERVAL_TEXT = re.compile(r"^(-?)(\d+)-(\d+) (-?\d+) (-?)(\d+):(\d+):(\d+)(?:\.(\d+))?$")
_FIELD_TYPES = {
    "INTEGER": "INT64", "INT64": "INT64", "FLOAT": "FLOAT64", "FLOAT64": "FLOAT64", "BOOLEAN": "BOOL", "BOOL": "BOOL",
    "RECORD": "STRUCT", "STRUCT": "STRUCT", "STRING": "STRING", "BYTES": "BYTES", "NUMERIC": "NUMERIC",
    "BIGNUMERIC": "BIGNUMERIC", "DATE": "DATE", "TIME": "TIME", "DATETIME": "DATETIME", "TIMESTAMP": "TIMESTAMP",
    "INTERVAL": "INTERVAL", "JSON": "JSON", "GEOGRAPHY": "GEOGRAPHY",
}


def field_type(field: dict) -> tuple:
    """A REST schema field as a type in the shape gsql_expected uses."""
    kind = _FIELD_TYPES.get(str(field.get("type", "")).upper(), "OTHER:" + str(field.get("type")))
    if kind == "STRUCT":
        base: tuple = ("STRUCT", tuple((f.get("name"), field_type(f)) for f in field.get("fields", [])))
    elif kind.startswith("OTHER:"):
        base = ("OTHER", kind[6:])
    else:
        base = (kind,)
    return ("ARRAY", base) if field.get("mode") == "REPEATED" else base


def _micros_of(fraction: str | None) -> int:
    return int((fraction or "").ljust(6, "0")[:6] or 0)


def actual_value(field: dict, cell: Any) -> Any:
    """One REST cell, decoded by the field's type into the values gsql_expected produces."""
    if field.get("mode") == "REPEATED":
        element = dict(field, mode="NULLABLE")
        return [actual_value(element, item.get("v")) for item in (cell or [])]
    if cell is None:
        return None
    kind = _FIELD_TYPES.get(str(field.get("type", "")).upper(), "OTHER")
    if kind == "STRUCT":
        return tuple(actual_value(sub, c.get("v")) for sub, c in zip(field.get("fields", []), cell.get("f", [])))
    if kind == "INT64":
        return int(cell)
    if kind == "BOOL":
        return str(cell).lower() == "true"
    if kind == "FLOAT64":
        return float(cell)
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(cell)
    if kind == "BYTES":
        import base64

        return base64.b64decode(cell)
    if kind == "TIMESTAMP":
        return int((Decimal(cell) * 1_000_000).to_integral_value())
    if kind == "DATE":
        return date.fromisoformat(cell)
    if kind == "TIME":
        m = _TIME_TEXT.match(cell)
        return dtime(int(m.group(1)), int(m.group(2)), int(m.group(3)), _micros_of(m.group(4)))
    if kind == "DATETIME":
        text = cell.replace("T", " ")
        base, _, fraction = text.partition(".")
        return datetime.fromisoformat(base).replace(microsecond=_micros_of(fraction))
    if kind == "INTERVAL":
        m = _INTERVAL_TEXT.match(cell)
        sign_ym, years, months, days, sign_t, hours, minutes, seconds, fraction = m.groups()
        total_months = int(years) * 12 + int(months)
        micros = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1_000_000 + _micros_of(fraction)
        return Interval(-total_months if sign_ym else total_months, int(days), -micros if sign_t else micros)
    return cell


_WIDE = {"INT32": "INT64", "UINT32": "INT64", "UINT64": "INT64", "FLOAT32": "FLOAT64"}


def types_match(expected: tuple, actual: tuple, loose: bool = False) -> bool:
    """Column types equal, ignoring struct field names (the emulator names columns its own way).

    ``loose`` also lets GoogleSQL's narrower number types (INT32, UINT32, UINT64, FLOAT32) match BigQuery's wide ones."""
    kind = _WIDE.get(expected[0], expected[0]) if loose else expected[0]
    if kind != actual[0]:
        return False
    if expected[0] == "ARRAY":
        return types_match(expected[1], actual[1], loose)
    if expected[0] == "STRUCT":
        return len(expected[1]) == len(actual[1]) and all(types_match(a, b, loose) for (_, a), (_, b) in zip(expected[1], actual[1]))
    return True


def _float_order(x: float) -> int:
    bits = struct.unpack(">q", struct.pack(">d", x))[0]
    return bits if bits >= 0 else -(1 << 63) - bits


def float_close(a: float, b: float, ulps: int = 4) -> bool:
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if a == b:
        return True  # -0.0 == 0.0 in GoogleSQL; the emulator's sign of a zero is not scored
    if math.isinf(a) or math.isinf(b):
        return False
    return abs(_float_order(a) - _float_order(b)) <= ulps


def _sort_key(value: Any) -> str:
    if isinstance(value, float):
        return "f:" + ("nan" if math.isnan(value) else f"{value:.8g}")
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_sort_key(v) for v in value) + "]"
    return repr(value)


def values_equal(expected: Any, actual: Any) -> bool:
    # BigQuery returns a NULL array as an empty one, so the two are the same result.
    if (expected is None and actual == []) or (actual is None and expected == []):
        return True
    if isinstance(expected, float) or isinstance(actual, float):
        if isinstance(expected, float) and isinstance(actual, float):
            return float_close(expected, actual)
        return False
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        if isinstance(expected, UnorderedList):
            expected, actual = sorted(expected, key=_sort_key), sorted(actual, key=_sort_key)
        return all(values_equal(e, a) for e, a in zip(expected, actual))
    if isinstance(expected, bool) != isinstance(actual, bool):
        return False
    return expected == actual


def rows_equal(expected: list, actual: list, unordered: bool) -> bool:
    if len(expected) != len(actual):
        return False
    if unordered:
        expected, actual = sorted(expected, key=_sort_key), sorted(actual, key=_sort_key)
    return all(values_equal(e, a) for e, a in zip(expected, actual))


_ZONE_STEP = 15 * 60 * 1_000_000
_ZONE_MAX = 14 * 3600 * 1_000_000


def _mask_timestamps(t: tuple, value: Any) -> Any:
    """``value`` with every TIMESTAMP cell replaced by ``None`` (a placeholder), walking the type."""
    if value is None:
        return None
    if t[0] == "TIMESTAMP":
        return "<ts>"
    if t[0] == "ARRAY":
        return [_mask_timestamps(t[1], v) for v in value]
    if t[0] == "STRUCT":
        return tuple(_mask_timestamps(ft, v) for (_, ft), v in zip(t[1], value))
    return value


def _timestamps(t: tuple, value: Any) -> list:
    if value is None:
        return []
    if t[0] == "TIMESTAMP":
        return [value]
    if t[0] == "ARRAY":
        return [x for v in value for x in _timestamps(t[1], v)]
    if t[0] == "STRUCT":
        return [x for (_, ft), v in zip(t[1], value) for x in _timestamps(ft, v)]
    return []


def zone_explained(row_type: tuple, expected: list, actual: list, unordered: bool) -> bool:
    """Whether the rows differ only in TIMESTAMP cells, each off by a time zone offset (a multiple of
    15 minutes, at most 14 hours): the expected values assume the reference driver's default zone."""
    masked_e = [_mask_timestamps(row_type, r) for r in expected]
    masked_a = [_mask_timestamps(row_type, r) for r in actual]
    order_e = sorted(range(len(expected)), key=lambda i: _sort_key(masked_e[i])) if unordered else list(range(len(expected)))
    order_a = sorted(range(len(actual)), key=lambda i: _sort_key(masked_a[i])) if unordered else list(range(len(actual)))
    if len(order_e) != len(order_a):
        return False
    differs = False
    for i, j in zip(order_e, order_a):
        if not values_equal(masked_e[i], masked_a[j]):
            return False
        ts_e, ts_a = _timestamps(row_type, expected[i]), _timestamps(row_type, actual[j])
        if len(ts_e) != len(ts_a):
            return False
        for a, b in zip(ts_e, ts_a):
            if a != b:
                differs = True
                if abs(a - b) % _ZONE_STEP or abs(a - b) > _ZONE_MAX:
                    return False
    return differs


def show(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


# --- the emulator side ----------------------------------------------------------------------------


@dataclass
class Outcome:
    file: str
    name: str
    verdict: str  # pass | wrong | missing_error | emulator_error | fixture | unscored | timeout
    reason: str = ""
    detail: str = ""
    split: str = "dev"
    claimed: bool = True
    params: bool = False
    tags: list[str] = field(default_factory=list)


_CREATE_TABLE = re.compile(r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.`]+)")
_CREATE_OTHER = re.compile(r"(?is)^\s*create\s+")


def query_parameters(client: Client, option: str | None, dataset: str) -> list[dict] | None:
    """Named REST parameters for a case's ``parameters`` option; ``None`` when a value is not a scalar."""
    params: list[dict] = []
    for entry in split_top_level(option or ""):
        match = re.match(r"^(.*\S)\s+as\s+(\w+)$", entry, re.I | re.S)
        if not match:
            return None
        result = client.query("SELECT " + match.group(1) + " AS v", dataset, decode=False)
        schema_field = result.schema["fields"][0]
        if schema_field.get("mode") == "REPEATED" or str(schema_field.get("type")).upper() in ("RECORD", "STRUCT"):
            return None
        kind = _FIELD_TYPES.get(str(schema_field["type"]).upper())
        if kind is None or not result.raw_rows:
            return None
        cell = result.raw_rows[0]["f"][0].get("v")
        value = {} if cell is None else {"value": str(cell)}
        params.append({"name": match.group(2), "parameterType": {"type": kind}, "parameterValue": value})
    return params


def split_top_level(text: str) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        char = text[i]
        if char in "\"'`":
            quote = char
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def result_rows(result) -> tuple[tuple, list]:
    """``(row type, rows)`` of an emulator result, in the values gsql_expected produces."""
    fields = result.schema.get("fields", [])
    row_type = ("STRUCT", tuple((f.get("name"), field_type(f)) for f in fields))
    rows = [tuple(actual_value(f, c.get("v")) for f, c in zip(fields, row.get("f", []))) for row in result.raw_rows]
    return row_type, rows


def compare(expected: Expected, result, loose: bool = False) -> tuple[str, str]:
    """``(verdict, reason)`` for rows the emulator returned against rows the reference expects.

    ``loose`` is for checking a built table against its printed rows: narrow number types match wide
    ones and the row order does not matter (a table has none)."""
    try:
        actual_type, actual_rows = result_rows(result)
    except (KeyError, ValueError, TypeError, AttributeError, ArithmeticError, InvalidOperation) as error:
        return "unreadable", f"result could not be read: {type(error).__name__}: {str(error)[:120]}"
    if expected.type[0] == "STRUCT":
        expected_rows, expected_type = expected.rows, expected.type
    else:  # a value table: one unnamed column
        expected_rows, expected_type = [(row,) for row in expected.rows], ("STRUCT", ((None, expected.type),))
    if (
        len(actual_type[1]) == 1
        and actual_type[1][0][1][0] == "STRUCT"
        and expected_type[0] == "STRUCT"
        and not types_match(expected_type, actual_type, True)
        and types_match(expected_type, actual_type[1][0][1], True)
    ):  # SELECT AS STRUCT: the reference prints the struct itself, a result column holds it
        actual_type, actual_rows = actual_type[1][0][1], [row[0] for row in actual_rows]
    if not types_match(expected_type, actual_type, True):
        return "wrong", f"column types differ: expected {show(expected_type, 160)}, got {show(actual_type, 160)}"
    if not rows_equal(expected_rows, actual_rows, expected.unordered or loose):
        if not loose and zone_explained(expected_type, expected_rows, actual_rows, expected.unordered):
            return "zone", "TIMESTAMP values differ by a time zone offset only (the reference ran in its default zone)"
        return "wrong", f"rows differ: expected {show(expected_rows)}, got {show(actual_rows)}"
    return "pass", ""


def _error_class(message: str) -> str:
    text = re.sub(r"\s+", " ", message)
    found = re.search(r"no such function: (\w+)", text)
    if found:
        return f"no such function: {found.group(1)}"
    found = re.search(r'"message":"([^"]{0,200})', text)
    return (found.group(1) if found else text[:200]).replace("\\", "")


class FileContext:
    """The tables of one test file, built in a scratch dataset and checked against their printed rows."""

    def __init__(self, client: Client, dataset: str, prepare: list[dict]):
        self.client = client
        self.dataset = dataset
        self.unloadable: dict[str, str] = {}
        self._blocked: re.Pattern | None = None
        for block in prepare:
            sql = block["sql"]
            match = _CREATE_TABLE.match(sql)
            if not match and not _CREATE_OTHER.match(sql):
                continue
            name = (match.group(1).replace("`", "") if match else re.sub(r"(?is)^\s*create\s+(?:or\s+replace\s+)?\w+\s+(?:if\s+not\s+exists\s+)?([\w.`]+).*", r"\1", sql, count=1).replace("`", ""))
            try:
                client.query(sql, dataset)
            except QueryError as error:
                self.unloadable[name.lower()] = f"setup rejected: {_error_class(str(error))}"
                continue
            if match and block["expected"]:
                try:
                    printed = parse_expected(block["expected"][0])
                    result = client.query(f"SELECT * FROM {name}", dataset, decode=False)
                except (ExpectedError, QueryError) as error:
                    if isinstance(error, QueryError):
                        self.unloadable[name.lower()] = f"setup unreadable: {_error_class(str(error))}"
                    continue
                if printed.kind == "rows" and printed.type[0] == "STRUCT":
                    verdict, reason = compare(printed, result, loose=True)
                    if verdict != "pass":
                        self.unloadable[name.lower()] = "setup reads back differently: " + reason[:200]

    def blocked(self, sql: str) -> str | None:
        if not self.unloadable:
            return None
        if self._blocked is None:
            names = sorted(self.unloadable, key=len, reverse=True)
            self._blocked = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.I)
        found = self._blocked.search(sql)
        return self.unloadable[found.group(1).lower()] if found else None


class EmulatorCrashed(RuntimeError):
    """The emulator stopped answering during a case (a crash or a hang): the case is the suspect."""


_NARROW = re.compile(r"'(INT32|UINT32|UINT64|FLOAT32)'")
_ZONE_FILES = re.compile(r"time_?zone")


def _zone_dependent(case: Case) -> bool:
    """The expected values were computed in a default time zone (the compliance driver's is America/Los_Angeles)."""
    return bool(
        _ZONE_FILES.search(case.file)
        or any(key in case.options for key in ("default_time_zone", "_time_zone", "time_zone"))
    )


def run_case(client: Client, context: FileContext, case: Case) -> Outcome:
    def done(verdict: str, reason: str = "", detail: str = "", params: bool = False) -> Outcome:
        return Outcome(case.file, case.name, verdict, reason, detail, case.split, case.claimed, params)

    blocked = context.blocked(case.sql)
    if blocked:
        return done("fixture", blocked)
    try:
        expected = parse_expected(case.expected)
    except NanoPrecision:
        return done("unscored", "expected result has sub-microsecond digits that BigQuery cannot represent")
    except ExpectedError as error:
        return done("unscored", f"expected result not readable: {str(error)[:100]}")
    if _zone_dependent(case):
        return done("zone", "the reference ran in a default time zone; BigQuery's default is UTC and the emulator cannot set one")
    if expected.kind == "rows" and _NARROW.search(repr(expected.type)):
        return done("unscored", "GoogleSQL-only number type (INT32, UINT32, UINT64, FLOAT32) in the expected result")
    params = None
    if case.options.get("parameters"):
        try:
            params = query_parameters(client, case.options["parameters"], context.dataset)
        except QueryError as error:
            return done("fixture", f"parameter: {_error_class(str(error))}", params=True)
        if params is None:
            return done("fixture", "a parameter that is not a scalar", params=True)
    has_params = params is not None
    try:
        result = client.query(case.sql, context.dataset, params, decode=False)
    except QueryError as error:
        if expected.kind == "error":
            return done("pass", "both raise an error", params=has_params)
        return done("emulator_error", _error_class(str(error)), params=has_params)
    except EmulatorUnavailable as error:
        raise EmulatorCrashed(f"{type(error).__name__}: {error}"[:200]) from None
    if expected.kind == "error":
        return done("missing_error", f"the reference raises {expected.code or 'an error'}; the emulator returns rows",
                    "", params=has_params)
    verdict, reason = compare(expected, result)
    if verdict == "unreadable":
        return done("emulator_error", reason[:90], reason, params=has_params)
    if verdict == "zone":
        return done("zone", reason, params=has_params)
    if verdict == "wrong":
        return done("wrong", reason.split(":")[0], reason, params=has_params)
    return done("pass", params=has_params)


def run_file(client: Client, name_cases: list[Case], prepare: list[dict]) -> tuple[list[Outcome], list[Case]]:
    """Run a file's cases. When the emulator dies on a case, that case is recorded as ``crash`` and the
    cases after it are handed back so the caller can restart the emulator and continue."""
    outcomes: list[Outcome] = []
    try:
        with client.scratch_dataset() as dataset:
            context = FileContext(client, dataset, prepare)
            for index, case in enumerate(name_cases):
                try:
                    outcomes.append(run_case(client, context, case))
                except EmulatorCrashed as error:
                    outcomes.append(Outcome(case.file, case.name, "crash", str(error), "", case.split, case.claimed))
                    return outcomes, name_cases[index + 1:]
    except EmulatorUnavailable as error:
        # The emulator died while the tables were built: nothing in this file can run on it.
        done = {o.name for o in outcomes}
        rest = [c for c in name_cases if c.name not in done]
        if rest and not outcomes:
            outcomes.append(Outcome(rest[0].file, rest[0].name, "crash", f"while building the file's tables: {error}"[:200], "", rest[0].split, rest[0].claimed))
            return outcomes, rest[1:]
    return outcomes, []
