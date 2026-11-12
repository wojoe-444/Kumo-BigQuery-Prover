"""Kinds carried through ``SELECT *``, set operations and unqualified names (issue #613, the limits left after #632).

A column compared with a string inside a derived table or CTE that selects ``*`` (or is written without its table) and with a
number outside it was proven unequal to itself: the kind inference followed named projections only. MySQL returns the
row (``'abc' = 0``), another engine raises a conversion error, and BigQuery rejects the comparison.
"""

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported import string_number_compare

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import SmtStatus, prove_equivalent_smt

PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)

SCHEMA = {"a": ["x", "k"], "b": ["y", "k"]}

NONE = "SELECT a.k FROM a WHERE FALSE"

WITNESSES = [
    ("SELECT d.k FROM (SELECT * FROM a WHERE a.x = 'abc') d JOIN b ON d.x = b.y WHERE b.y = 0", "SELECT d.k FROM (SELECT * FROM a WHERE FALSE) d"),
    (
        "SELECT d.k FROM (SELECT * FROM a WHERE a.x = 'abc') d JOIN (SELECT * FROM b WHERE b.y = 0) e ON d.x = e.y",
        "SELECT d.k FROM (SELECT * FROM a WHERE FALSE) d",
    ),
    ("WITH d AS (SELECT * FROM a WHERE a.x = 'abc') SELECT d.k FROM d WHERE d.x = 0", NONE),
    ("SELECT d.k FROM (SELECT a.* FROM a JOIN b ON a.k = b.k WHERE a.x = 'abc') d WHERE d.x = 0", NONE),
    # a column written without its table is the one the query qualifies elsewhere
    ("SELECT k FROM a WHERE x = 'abc' AND a.x = 0", "SELECT k FROM a WHERE FALSE"),
    ("SELECT a.k FROM a JOIN b ON x = y WHERE a.x = 'abc' AND y = 0", NONE),
    # set operations: the position of a column in every branch
    (
        "SELECT d.v FROM (SELECT a.x AS v FROM a WHERE a.x = 'abc' UNION ALL SELECT b.y FROM b WHERE b.y = 5) d WHERE d.v = 0",
        "SELECT d.v FROM (SELECT a.x AS v FROM a WHERE FALSE UNION ALL SELECT b.y FROM b WHERE b.y = 5) d WHERE d.v = 0",
    ),
    ("SELECT d.k FROM (SELECT b.k, b.y AS v FROM b UNION ALL SELECT a.k, a.x FROM a) d WHERE d.v = 'abc' AND d.v = 0", NONE),
    ("SELECT b.k FROM b WHERE b.y IN (SELECT a.x FROM a UNION ALL SELECT b2.y FROM b b2) AND b.y = 0 AND b.y = 'abc'", NONE),
]

NEAR_MISSES = [
    ("SELECT d.k FROM (SELECT * FROM a WHERE a.k = 1) d JOIN (SELECT * FROM b WHERE b.y = 2) e ON d.k = e.y", "SELECT d.k FROM (SELECT * FROM a WHERE FALSE) d"),
    ("SELECT k FROM a WHERE k = 1 AND a.k = 2", "SELECT k FROM a WHERE FALSE"),
    # a star over a table whose columns the query only compares with their own kind
    ("SELECT d.k FROM (SELECT * FROM a) d WHERE d.k = 1 AND d.k = 2", "SELECT d.k FROM (SELECT * FROM a) d WHERE FALSE"),
]

TEXT_COLUMNS = [
    ("SELECT d.k FROM (SELECT * FROM a WHERE a.x = 'abc') d JOIN b ON d.x = b.y WHERE b.y = 'abd'", "SELECT d.k FROM (SELECT * FROM a WHERE FALSE) d"),
    ("SELECT k FROM a WHERE x = 'abc' AND a.x = 'abd'", "SELECT k FROM a WHERE FALSE"),
]

TYPES = {"a": {"x": "STRING"}, "b": {"y": "STRING"}}

@pytest.mark.parametrize("left,right", WITNESSES)
def test_the_inference_names_the_comparison(left, right):
    assert string_number_compare.problem(left, "mysql", plain_ok=True)

def test_set_operation_branches_are_all_read():
    sql = "SELECT d.v FROM (SELECT a.x AS v FROM a UNION ALL SELECT b.y FROM b WHERE b.y = 5) d WHERE d.v = 'abc'"
    assert string_number_compare.problem(sql, "mysql", plain_ok=True)
    assert string_number_compare.problem(sql.replace("b.y = 5", "b.y = 'q'"), "mysql", plain_ok=True) is None
