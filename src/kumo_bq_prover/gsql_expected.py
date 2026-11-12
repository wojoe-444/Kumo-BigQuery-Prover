"""Read the expected results of the GoogleSQL compliance tests.

The compliance files print a result as ``ARRAY<STRUCT<a INT64, b STRING>>[known order:{1, "x"}, ...]``:
the type of the rows, then the rows. Values follow the header's types (a nested array repeats its
own type, ``ARRAY<INT64>[unknown order:1, 2]``, because the header only says ``ARRAY<>``).

The reader is a port of the one in KumoSQL's ``tools/googlesql_conformance.py`` that keeps no
other dependency: types are plain tuples and values are plain Python objects.

* a type is ``("INT64",)``, ``("ARRAY", elem)``, ``("STRUCT", ((name, type), ...))`` or ``("OTHER", text)``
* INT64 -> ``int``, FLOAT64 -> ``float``, NUMERIC and BIGNUMERIC -> ``Decimal``, BOOL -> ``bool``,
  STRING -> ``str``, BYTES -> ``bytes``, DATE -> ``datetime.date``, TIME -> ``datetime.time``,
  DATETIME -> ``datetime.datetime`` (no zone), TIMESTAMP -> ``int`` microseconds since the epoch,
  INTERVAL -> ``Interval``, ARRAY -> ``list`` (``UnorderedList`` when printed ``unknown order``),
  STRUCT -> ``tuple``, NULL -> ``None``; anything the runner does not model -> ``Raw`` text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

Type = tuple


class ExpectedError(Exception):
    """The expected-result text is not in a form this reader understands."""


class NanoPrecision(ExpectedError):
    """The expected result has sub-microsecond digits (GoogleSQL's nanosecond types); BigQuery has none."""


class Raw(str):
    """The printed text of a value of a type the reader does not model (a protocol buffer, a range...)."""

    __slots__ = ()


class UnorderedList(list):
    """An array the reference printed as ``unknown order``: compare as a multiset."""


@dataclass(frozen=True)
class Interval:
    months: int
    days: int
    micros: int


@dataclass
class Expected:
    kind: str  # "rows" or "error"
    type: Type | None = None  # the type of one row (a STRUCT) or of one value (a value table)
    rows: list = field(default_factory=list)
    unordered: bool = False  # the printed rows say ``unknown order``
    code: str = ""  # an error's status code (``out_of_range``)
    message: str = ""
    nondeterministic: bool = False  # the reference reported non-determinism: the rows are one possible answer


SCALARS = {
    "INT64", "INT32", "UINT32", "UINT64", "FLOAT64", "FLOAT32", "NUMERIC", "BIGNUMERIC", "BOOL", "STRING",
    "BYTES", "DATE", "TIME", "DATETIME", "TIMESTAMP", "INTERVAL", "JSON", "GEOGRAPHY",
}
ALIASES = {"DOUBLE": "FLOAT64", "FLOAT": "FLOAT32", "INT": "INT64", "INTEGER": "INT64", "BOOLEAN": "BOOL", "DECIMAL": "NUMERIC",
           "BIGDECIMAL": "BIGNUMERIC", "TIMESTAMP_PICOS": "OTHER"}
PLACEHOLDER: Type = ("OTHER", "?")

_PREFIX = re.compile(r"(ARRAY|STRUCT|PROTO|ENUM|RANGE|MAP)\s*<")
_OPENERS = "([{"
_CLOSERS = ")]}"


def has_placeholder(t: Type) -> bool:
    if t == PLACEHOLDER:
        return True
    if t[0] == "ARRAY":
        return has_placeholder(t[1])
    if t[0] == "STRUCT":
        return any(has_placeholder(ft) for _, ft in t[1])
    return False


def merge_type(known: Type, seen: Type) -> Type:
    """``known`` with its ``ARRAY<>`` placeholders filled in from ``seen`` (a type read from a value)."""
    if not has_placeholder(known):
        return known
    if known == PLACEHOLDER:
        return seen
    if known[0] == "ARRAY" and seen[0] == "ARRAY":
        return ("ARRAY", merge_type(known[1], seen[1]))
    if known[0] == "STRUCT" and seen[0] == "STRUCT" and len(known[1]) == len(seen[1]):
        return ("STRUCT", tuple((n, merge_type(a, b)) for (n, a), (_, b) in zip(known[1], seen[1])))
    return known


def parse_header_type(text: str) -> Type:
    """A type as the compliance printer writes it (``STRUCT<a INT64, ARRAY<>>``; a bare ``ARRAY<>`` is a placeholder)."""
    pos = 0
    n = len(text)

    def fail(message: str):
        raise ExpectedError(f"type {text[:80]!r}: {message}")

    def skip():
        nonlocal pos
        while pos < n and text[pos].isspace():
            pos += 1

    def word() -> str:
        nonlocal pos
        skip()
        start = pos
        while pos < n and (text[pos].isalnum() or text[pos] in "_.$"):
            pos += 1
        return text[start:pos]

    def balanced() -> str:
        nonlocal pos
        start = pos
        depth = 0
        while pos < n:
            depth += {"<": 1, ">": -1}.get(text[pos], 0)
            pos += 1
            if depth == 0:
                return text[start:pos]
        fail("unbalanced <>")

    def field_end(start: int) -> int:
        depth = 0
        i = start
        while i < n:
            char = text[i]
            if char == "<":
                depth += 1
            elif char == ">":
                if depth == 0:
                    return i
                depth -= 1
            elif char == "," and depth == 0:
                return i
            i += 1
        fail("unterminated STRUCT")

    def split_field(chunk: str) -> tuple[str | None, Type]:
        # ``name type`` or ``type``. The printer writes the name as it is, so it may hold spaces
        # (``GROUPING SETS INT64``): the type is what ends the field, the name everything before it.
        chunk = chunk.strip()
        if not chunk:
            fail("an empty STRUCT field")
        if chunk.endswith(">"):
            depth = 0
            start = len(chunk)
            for i in range(len(chunk) - 1, -1, -1):
                if chunk[i] == ">":
                    depth += 1
                elif chunk[i] == "<":
                    depth -= 1
                    if depth == 0:
                        start = i
                        break
            else:
                fail("unbalanced <> in a STRUCT field")
            while start > 0 and chunk[start - 1].isspace():
                start -= 1
            while start > 0 and (chunk[start - 1].isalnum() or chunk[start - 1] in "_.$"):
                start -= 1
        else:
            start = 0
            for i in range(len(chunk) - 1, -1, -1):
                if chunk[i].isspace():
                    start = i + 1
                    break
        name = chunk[:start].strip()
        return (name or None), parse_header_type(chunk[start:])

    def one() -> Type:
        nonlocal pos
        name = word()
        if not name:
            fail(f"a type name expected at {pos}")
        upper = name.upper()
        skip()
        has_args = pos < n and text[pos] == "<"
        if upper == "ARRAY" and has_args:
            pos += 1
            skip()
            if pos < n and text[pos] == ">":
                pos += 1
                return ("ARRAY", PLACEHOLDER)
            elem = one()
            skip()
            if pos >= n or text[pos] != ">":
                fail("'>' expected")
            pos += 1
            return ("ARRAY", elem)
        if upper == "STRUCT" and has_args:
            pos += 1
            fields = []
            while True:
                skip()
                if pos >= n:
                    fail("unterminated STRUCT")
                if text[pos] == ">":
                    pos += 1
                    break
                end = field_end(pos)
                fields.append(split_field(text[pos:end]))
                pos = end
                skip()
                if pos < n and text[pos] == ",":
                    pos += 1
            return ("STRUCT", tuple(fields))
        if has_args:  # PROTO<..>, ENUM<..>, RANGE<..>, MAP<..>: kept as text
            return ("OTHER", name.upper() + balanced())
        upper = ALIASES.get(upper, upper)
        if upper in SCALARS:
            return (upper,)
        return ("OTHER", upper)

    result = one()
    skip()
    if pos != n:
        fail(f"text after the type at {pos}")
    return result


_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"',
            "?": "?", "`": "`"}


def unescape_literal(body: str, is_bytes: bool) -> str | bytes:
    """The text between the quotes of a GoogleSQL string or bytes literal, escapes decoded."""
    out: list[int] = []
    i = 0
    n = len(body)
    while i < n:
        char = body[i]
        if char != "\\":
            if is_bytes:
                out.extend(char.encode("utf-8"))
            else:
                out.append(ord(char))
            i += 1
            continue
        i += 1
        if i >= n:
            raise ExpectedError("literal ends with a backslash")
        char = body[i]
        if char in _ESCAPES:
            out.append(ord(_ESCAPES[char]))
            i += 1
        elif char in "xX":
            digits = body[i + 1:i + 3]
            if not re.fullmatch(r"[0-9a-fA-F]{2}", digits):
                raise ExpectedError("bad \\x escape")
            out.append(int(digits, 16))
            i += 3
        elif char in "uU" and not is_bytes:
            width = 4 if char == "u" else 8
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise ExpectedError("bad unicode escape")
            out.append(int(digits, 16))
            i += 1 + width
        elif char in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i:]).group(0)
            out.append(int(digits, 8))
            i += len(digits)
        else:
            raise ExpectedError(f"unknown escape \\{char}")
    if is_bytes:
        return bytes(out)
    try:
        return "".join(map(chr, out))
    except (ValueError, OverflowError):
        raise ExpectedError("code point out of range") from None


_DATETIME_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d+))?$")
_TIMESTAMP_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d(?::?\d\d)?)?$")
_TIME_TEXT = re.compile(r"^(\d\d):(\d\d):(\d\d)(?:\.(\d+))?$")
_INTERVAL_TEXT = re.compile(r"^(-?)(\d+)-(\d+) (-?\d+) (-?)(\d+):(\d+):(\d+)(?:\.(\d+))?$")
EPOCH = datetime(1970, 1, 1)


def _micros(fraction: str | None) -> int:
    if not fraction:
        return 0
    if len(fraction) > 6 and fraction[6:].strip("0"):
        raise NanoPrecision("sub-microsecond value")
    return int(fraction[:6].ljust(6, "0"))


def utc_to_micros(civil: datetime) -> int:
    delta = civil - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _seconds(value: int) -> tuple[int, int]:
    """Seconds equal to 60 roll into the next minute (a leap second as the reference prints it)."""
    return (59, 1) if value == 60 else (value, 0)


def scalar_value(kind: str, token: str) -> Any:
    """A printed scalar of type ``kind`` as its payload."""
    try:
        if kind in ("INT64", "INT32", "UINT32", "UINT64"):
            return int(token)
        if kind in ("FLOAT64", "FLOAT32"):
            return float(token)
        if kind in ("NUMERIC", "BIGNUMERIC"):
            value = Decimal(token)
            if not value.is_finite():
                raise ValueError(token)
            return value
        if kind == "BOOL":
            if token not in ("true", "false"):
                raise ValueError(token)
            return token == "true"
        if kind == "DATE":
            return date.fromisoformat(token)
        if kind == "TIME":
            m = _TIME_TEXT.match(token)
            s, extra = _seconds(int(m.group(3)))
            base = datetime(2000, 1, 1, int(m.group(1)), int(m.group(2)), s, _micros(m.group(4))) + timedelta(seconds=extra)
            return base.time()
        if kind == "DATETIME":
            m = _DATETIME_TEXT.match(token)
            y, mo, d, h, mi, s = map(int, m.groups()[:6])
            s, extra = _seconds(s)
            return datetime(y, mo, d, h, mi, s, _micros(m.group(7))) + timedelta(seconds=extra)
        if kind == "TIMESTAMP":
            m = _TIMESTAMP_TEXT.match(token)
            y, mo, d, h, mi, s = map(int, m.groups()[:6])
            s, extra = _seconds(s)
            civil = datetime(y, mo, d, h, mi, s, _micros(m.group(7))) + timedelta(seconds=extra)
            offset = m.group(8)
            minutes = 0
            if offset and offset != "Z":
                digits = offset[1:].replace(":", "")
                minutes = int(digits[:2]) * 60 + (int(digits[2:]) if len(digits) > 2 else 0)
                minutes = -minutes if offset[0] == "-" else minutes
            return utc_to_micros(civil - timedelta(minutes=minutes))
        if kind == "INTERVAL":
            m = _INTERVAL_TEXT.match(token)
            sign_ym, years, months, days, sign_t, hours, minutes, seconds, fraction = m.groups()
            total_months = int(years) * 12 + int(months)
            micros = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1_000_000 + _micros(fraction)
            return Interval(-total_months if sign_ym else total_months, int(days), -micros if sign_t else micros)
    except NanoPrecision:
        raise
    except (ValueError, AttributeError, ArithmeticError, TypeError, OverflowError):
        raise ExpectedError(f"bad {kind} value {token[:60]!r}") from None
    raise ExpectedError(f"no reader for a {kind} value")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def fail(self, message: str):
        raise ExpectedError(f"{message} at {self.pos}: {self.text[max(0, self.pos - 20):self.pos + 30]!r}")

    def ws(self) -> None:
        text, n = self.text, len(self.text)
        while self.pos < n and text[self.pos].isspace():
            self.pos += 1

    def read_type(self) -> Type:
        text = self.text
        start = self.pos
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "<":
                depth += 1
            elif text[i] == ">":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        else:
            self.fail("unterminated type")
        self.pos = i + 1
        return parse_header_type(text[start:i + 1])

    def at_null(self) -> bool:
        text, pos = self.text, self.pos
        return text.startswith("NULL", pos) and (pos + 4 >= len(text) or text[pos + 4] in ",}])" or text[pos + 4].isspace())

    def raw(self) -> str:
        text, n = self.text, len(self.text)
        start = self.pos
        depth = 0
        i = start
        while i < n:
            char = text[i]
            if char in "\"'`":
                i = self.skip_quoted(i)
                continue
            if char in _OPENERS:
                depth += 1
            elif char in _CLOSERS:
                if depth == 0:
                    break
                depth -= 1
            elif char == "," and depth == 0:
                break
            i += 1
        self.pos = i
        return text[start:i].strip()

    def skip_quoted(self, i: int) -> int:
        text = self.text
        quote = text[i]
        if text.startswith(quote * 3, i):
            end = text.find(quote * 3, i + 3)
            if end < 0:
                self.fail("unterminated string")
            return end + 3
        i += 1
        while i < len(text) and text[i] != quote:
            i += 2 if text[i] == "\\" else 1
        if i >= len(text):
            self.fail("unterminated string")
        return i + 1

    def literal(self, is_bytes: bool) -> str | bytes:
        text = self.text
        if is_bytes:
            if text[self.pos] in "bB":
                self.pos += 1
            else:
                self.fail("bytes literal expected")
        if self.pos >= len(text) or text[self.pos] not in "\"'":
            self.fail("string literal expected")
        start = self.pos
        end = self.skip_quoted(start)
        self.pos = end
        quote = text[start]
        width = 3 if text.startswith(quote * 3, start) else 1
        return unescape_literal(text[start + width:end - width], is_bytes)

    def value(self, hint: Type | None) -> tuple[Type, Any]:
        self.ws()
        text = self.text
        if _PREFIX.match(text, self.pos):
            hint = self.read_type()
            self.ws()
            if text.startswith("(NULL)", self.pos):
                self.pos += 6
                return hint, None
        if self.at_null():
            self.pos += 4
            return (hint or PLACEHOLDER), None
        if hint is None:
            self.fail("a value with no type")
        kind = hint[0]
        if kind == "ARRAY":
            if not text.startswith("[", self.pos):
                self.fail("array expected")
            return self.array(hint)
        if kind == "STRUCT":
            if not text.startswith("{", self.pos):
                self.fail("struct expected")
            return self.struct(hint)
        if kind in ("OTHER", "JSON", "GEOGRAPHY"):
            return hint, Raw(self.raw())
        if kind == "STRING":
            return hint, self.literal(False)
        if kind == "BYTES":
            return hint, self.literal(True)
        token = self.raw()
        return hint, scalar_value(kind, token)

    def array(self, hint: Type) -> tuple[Type, Any]:
        text = self.text
        self.pos += 1
        self.ws()
        unordered = False
        for marker in ("unknown order:", "known order:"):
            if text.startswith(marker, self.pos):
                self.pos += len(marker)
                unordered = marker.startswith("unknown")
                break
        elem = hint[1]
        items = []
        while True:
            self.ws()
            if self.pos >= len(text):
                self.fail("unterminated array")
            if text[self.pos] == "]":
                self.pos += 1
                break
            seen, item = self.value(hint[1])
            elem = merge_type(elem, seen)
            items.append(item)
            self.ws()
            if self.pos < len(text) and text[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(text) and text[self.pos] != "]":
                self.fail("',' or ']' expected")
        payload = UnorderedList(items) if unordered else list(items)
        return ("ARRAY", elem), payload

    def struct(self, hint: Type) -> tuple[Type, Any]:
        text = self.text
        self.pos += 1
        fields, items = [], []
        for index, (name, field_type) in enumerate(hint[1]):
            self.ws()
            seen, item = self.value(field_type)
            fields.append((name, merge_type(field_type, seen)))
            items.append(item)
            self.ws()
            if index + 1 < len(hint[1]):
                if text[self.pos:self.pos + 1] != ",":
                    self.fail("',' expected between struct fields")
                self.pos += 1
        self.ws()
        if text[self.pos:self.pos + 1] != "}":
            self.fail("'}' expected")
        self.pos += 1
        return ("STRUCT", tuple(fields)), tuple(items)


def parse_expected(text: str) -> Expected:
    """The result a compliance case expects: an error (``ERROR: generic::out_of_range: ...``) or typed rows."""
    text = text.strip()
    if text.startswith("ERROR"):
        match = re.match(r"ERROR:\s*(?:generic::)?(\w+)?:?\s*(.*)", text, re.S)
        return Expected("error", code=match.group(1) or "", message=match.group(2))
    note = re.search(r"\n\s*NOTE:[^\n]*$", text)
    if note:
        text = text[:note.start()].rstrip()
    parser = _Parser(text)
    parser.ws()
    if not _PREFIX.match(text, parser.pos):
        raise ExpectedError("result does not start with a type")
    result_type, payload = parser.value(None)
    parser.ws()
    if parser.pos != len(text):
        parser.fail("text after the result")
    if result_type[0] != "ARRAY" or not isinstance(payload, list):
        raise ExpectedError(f"result is a {result_type[0]}, not an array of rows")
    return Expected("rows", result_type[1], list(payload), isinstance(payload, UnorderedList), nondeterministic=bool(note))
