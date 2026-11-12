"""``x + INTERVAL ..`` is a date, a timestamp or an interval, not a number (``kumo_bq_prover.ported.string_number_compare``).

``d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '60' DAY`` compares a date column with a date string
and a date. The guard against a string compared with a number read the ``+`` as arithmetic on numbers and declined every
query with such a bound, even against itself. Arithmetic on numbers is still declined.
"""

import datetime

import random

import pytest

import sqlglot

pytest.importorskip("z3")

from kumo_bq_prover.ported import string_number_compare

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

SCHEMA = {"t": ["k", "d", "n"], "u": ["k", "d", "n"]}

TYPES = {name: {"k": "INT", "d": "DATE", "n": "INT"} for name in SCHEMA}

BOUND = "CAST('2000-10-01' AS DATE) + INTERVAL '60' DAY"

def proved(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven

def day(text):
    return datetime.date.fromisoformat(text)

TARGETS = [
    # the bound against itself, under an alias and under a derived table
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT x.k FROM t AS x WHERE x.d BETWEEN '2000-10-01' AND {BOUND}"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM (SELECT * FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}) AS x"),
    # BETWEEN is its two comparisons
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM t WHERE d >= '2000-10-01' AND d <= {BOUND}"),
    # a filter pushed below a join
    (
        f"SELECT t.k, COUNT(*) AS c FROM t JOIN u ON t.k = u.k WHERE t.d BETWEEN '2000-10-01' AND {BOUND} GROUP BY t.k",
        f"SELECT x.k, COUNT(*) AS c FROM (SELECT * FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}) AS x JOIN u ON x.k = u.k GROUP BY x.k",
    ),
]

@pytest.mark.parametrize("left, right", TARGETS)
def test_interval_bounds_prove(left, right):
    assert proved(left, right)

ROWS = {
    "t": [(1, day("2000-10-01"), 1), (2, day("2000-10-20"), 2), (3, day("2000-11-15"), 3), (4, day("2000-11-30"), 4), (5, day("2000-12-01"), 5), (6, None, 6), (2, day("2000-10-20"), 2)],
    "u": [(1, None, 1), (2, None, 2), (3, None, 3), (4, None, 4), (5, None, 5)],
}

NEAR_MISSES = [
    # another length, another unit, the other sign, a strict bound, no upper bound
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '30' DAY"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '60' MONTH"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) - INTERVAL '60' DAY"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM t WHERE d >= '2000-10-01' AND d < {BOUND}"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d >= '2000-10-01'"),
    # another start date
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-09-01' AS DATE) + INTERVAL '60' DAY"),
]
