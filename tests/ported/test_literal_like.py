"""``x LIKE 'abc'`` read as ``x = 'abc'`` (``literal_like``), where the two compare strings the same way."""

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

SCHEMA = {"s": ["id", "name", "code"]}

TYPES = {"s": {"id": "INTEGER", "name": "VARCHAR", "code": "CHAR(5)"}}

def proven(left, right, dialect="bigquery", types=None):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types, dialect=dialect, compare_names=False).proven

NEAR_MISSES = [
    ("SELECT id FROM s WHERE name LIKE 'abc%'", "SELECT id FROM s WHERE name = 'abc%'"),
    ("SELECT id FROM s WHERE name LIKE 'a_c'", "SELECT id FROM s WHERE name = 'a_c'"),
    ("SELECT id FROM s WHERE name LIKE 'a\\\\%c'", "SELECT id FROM s WHERE name = 'a\\\\%c'"),
    ("SELECT id FROM s WHERE name LIKE 'abc'", "SELECT id FROM s WHERE name = 'ABC'"),
    ("SELECT id FROM s WHERE name LIKE 'abc'", "SELECT id FROM s WHERE name = 'abc '"),
    ("SELECT id FROM s WHERE name LIKE 'abc'", "SELECT id FROM s WHERE name = 'abc' OR name IS NULL"),
    ("SELECT id FROM s WHERE name NOT LIKE 'abc'", "SELECT id FROM s WHERE name <> 'abc' OR name IS NULL"),
]

@pytest.mark.parametrize("left, right", NEAR_MISSES)
@pytest.mark.parametrize("dialect, types", [("bigquery", None), ("postgres", TYPES)])
def test_near_misses_are_not_proved(left, right, dialect, types):
    assert not proven(left, right, dialect, types)

def test_the_rule_is_not_applied_where_like_and_equality_compare_differently():
    left, right = "SELECT id FROM s WHERE name LIKE 'abc'", "SELECT id FROM s WHERE name = 'abc'"
    assert not proven(left, right, "mysql", TYPES)  # = ignores trailing spaces under a PAD SPACE collation
    assert not proven(left, right, "sqlite", TYPES)  # LIKE ignores ASCII case
    assert not proven(left, right, "postgres", None)  # the column may be CHAR(n)
    padded = "SELECT id FROM s WHERE code LIKE 'abc'", "SELECT id FROM s WHERE code = 'abc'"
    assert not proven(*padded, "postgres", TYPES)
    derived = "SELECT id FROM (SELECT id, code AS name FROM s) AS d WHERE name LIKE 'abc'"
    assert "LIKE" in normalize(derived, schema=SCHEMA, types=TYPES, dialect="postgres").upper()
