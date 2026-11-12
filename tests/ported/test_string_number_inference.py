"""String-versus-number comparisons the #542 fix missed (issue #613), and the exact integer strings now read as numbers.

On master the provers proved each ``WITNESSES`` pair equivalent although MySQL returns different rows (a string column
holding ``'abc'`` joins a number column holding ``0``, since MySQL reads ``'abc'`` as 0; ``COALESCE(n, 'abc') = 0`` is true for a
NULL ``n``). another engine raises a conversion or binder error on the same SQL and BigQuery rejects it as a type error, so no engine
returns the rows the proofs claimed. The inference in ``kumo_bq_prover.ported.string_number_compare`` declines them; the near misses that
compare one kind with itself stay proven. ``kumo_bq_prover.ported.string_number_literals`` reads small integer strings as numbers where MySQL, another engine and
PostgreSQL agree.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported import string_number_compare, string_number_literals

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import SmtStatus, prove_equivalent_smt

PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)

NONE = "SELECT a.k FROM a WHERE FALSE"

TYPES = {"a": {"x": "VARCHAR", "n": "INT", "k": "INT"}, "b": {"y": "INT", "n": "VARCHAR", "k": "INT"}}

WITNESSES = [
    # a column compared with a string on one side of a join and a number on the other
    ("SELECT a.k FROM a JOIN b ON a.x = b.y WHERE a.x = 'abc' AND b.y = 0", NONE, None),
    ("SELECT a.k FROM a JOIN b ON b.y = a.x WHERE b.y = 0 AND a.x = 'abc'", NONE, None),
    ("SELECT a.k FROM a JOIN b ON a.x < b.y WHERE a.x = 'abc' AND b.y = 0", NONE, None),
    ("SELECT a.k FROM a JOIN b USING (x) WHERE a.x = 'abc' AND b.x = 0", "SELECT a.k FROM a JOIN b USING (x) WHERE FALSE", None),
    ("SELECT a.k FROM a WHERE a.x IN (SELECT b.y FROM b WHERE b.y = 0) AND a.x = 'abc'", NONE, None),
    ("SELECT a.k FROM a WHERE a.x = a.n + 0 AND a.x = 'abc'", NONE, None),
    # derived tables and CTEs carry the kind through their projections
    (
        "SELECT d.k FROM (SELECT a.k, a.x AS v FROM a WHERE a.x = 'abc') d JOIN (SELECT b.y AS w FROM b WHERE b.y = 0) e ON d.v = e.w",
        "SELECT d.k FROM (SELECT a.k FROM a WHERE FALSE) d",
        None,
    ),
    ("WITH d AS (SELECT a.x AS v FROM a WHERE a.x = 'abc') SELECT d.v FROM d WHERE d.v = 0", "SELECT d.v FROM (SELECT a.x AS v FROM a WHERE FALSE) d", None),
    # a value that mixes kinds
    ("SELECT a.k FROM a WHERE COALESCE(a.x, 0) = 'abc' AND COALESCE(a.x, 0) = 0", NONE, None),
    ("SELECT a.k FROM a WHERE COALESCE(a.n, 'abc') = 0 AND a.n IS NULL", NONE, None),
    ("SELECT a.k FROM a WHERE COALESCE(NULL, 'abc') = 0", NONE, None),
    ("SELECT a.k FROM a WHERE IFNULL(a.n, 'abc') = 0 AND a.n IS NULL", NONE, None),
    ("SELECT a.k FROM a WHERE CASE WHEN a.k > 0 THEN 'abc' ELSE 'x' END = 0", NONE, None),
    ("SELECT a.k FROM a WHERE (CASE WHEN a.k > 0 THEN 'abc' ELSE 0 END) = 0", "SELECT a.k FROM a WHERE a.k IS NULL OR a.k <= 0", None),
    ("SELECT a.k FROM a WHERE IF(a.k > 5, 'abc', 0) = 0 AND a.k > 5", NONE, None),
    ("SELECT a.k FROM a WHERE NULLIF(a.x, 'abc') = 0 AND a.x = 'abc'", NONE, None),
    ("SELECT a.k FROM a WHERE GREATEST('abc', 0) = 0", NONE, None),
    ("SELECT a.k FROM a WHERE COALESCE(a.x, 'p') + 1 = 2", NONE, None),
    # an expression whose kind the text settles
    ("SELECT a.k FROM a WHERE LOWER(a.x) = 0", NONE, None),
    ("SELECT a.k FROM a GROUP BY a.k HAVING SUM(a.n) = 'abc'", "SELECT a.k FROM a GROUP BY a.k HAVING FALSE", None),
    # declared types are read per table, not per bare column name
    ("SELECT a.k FROM a JOIN b ON a.k = b.k WHERE a.n = 0 AND b.n = 'abc' AND a.n = b.n", "SELECT a.k FROM a JOIN b ON FALSE", TYPES),
    ("SELECT a.k FROM a JOIN b ON a.x = b.y", "SELECT a.k FROM a JOIN b ON FALSE", TYPES),
]

NEAR_MISSES = [
    ("SELECT a.k FROM a JOIN b ON a.x = b.y WHERE a.x = 1 AND b.y = 2", NONE, None),
    ("SELECT a.k FROM a JOIN b ON a.x = b.y WHERE a.x = 1 AND b.y = 2", NONE, {"a": {"x": "INT", "k": "INT"}, "b": {"y": "INT"}}),
    ("SELECT a.k FROM a WHERE COALESCE(a.x, 0) = 1 AND a.x IS NULL", NONE, None),
    ("SELECT a.k FROM a WHERE COALESCE(a.x, 'p') = 'q' AND a.x IS NULL", NONE, None),
    ("SELECT a.k FROM a WHERE COALESCE(a.x, 'p') = a.z AND a.z = 'q' AND a.x IS NULL", NONE, None),
    ("SELECT a.k FROM a WHERE CASE WHEN a.k > 0 THEN 'p' ELSE 'q' END = 'r'", NONE, None),
    ("SELECT a.k FROM a WHERE CASE WHEN a.k > 0 THEN 1 ELSE 2 END = 3", NONE, None),
    # the same column name in two tables with nothing joining the two
    ("SELECT a.k FROM a JOIN b ON a.k = b.k WHERE a.x = 'abc' AND b.x = 'abd' AND a.x = 'abc'", "SELECT a.k FROM a JOIN b ON a.k = b.k WHERE a.x = 'abc' AND b.x = 'abd'", None),
]

TEXT_ONLY = [
    ("SELECT a.k FROM a JOIN b ON a.x = b.y WHERE a.x = 'abc' AND b.y = 'abd'", NONE, None),
    ("SELECT a.k FROM a JOIN b ON a.k = b.k WHERE a.x = 'abc' AND a.x = 'abd' AND b.x = 0", NONE, None),
    (
        "SELECT d.k FROM (SELECT a.k, a.x AS v FROM a WHERE a.x = 'abc') d JOIN (SELECT b.y AS w FROM b WHERE b.y = 'abd') e ON d.v = e.w",
        "SELECT d.k FROM (SELECT a.k FROM a WHERE FALSE) d",
        None,
    ),
]

def test_problem_names_what_it_found():
    assert "compared with a number" in string_number_compare.problem(WITNESSES[0][0], "mysql", plain_ok=True)
    assert "mixed kinds" in string_number_compare.problem("SELECT a.k FROM a WHERE COALESCE(0, 'x') + 1 = a.k", "mysql", plain_ok=True)
    assert string_number_compare.problem(NEAR_MISSES[0][0], "mysql", plain_ok=True) is None

FOLDED = [
    ("SELECT a.k FROM a WHERE '2' = 2", "SELECT a.k FROM a WHERE TRUE", True),
    ("SELECT a.k FROM a WHERE '2' <> 2", NONE, True),
    ("SELECT a.k FROM a WHERE '2' < 3", "SELECT a.k FROM a", True),
    ("SELECT a.k FROM a WHERE 3 <= '2'", NONE, True),
    ("SELECT a.k FROM a WHERE '-5' < 0", "SELECT a.k FROM a", True),
    ("SELECT a.k FROM a WHERE '2' <> 2", "SELECT a.k FROM a", False),
    ("SELECT a.k FROM a WHERE '2' = 3", "SELECT a.k FROM a", False),
]

COLUMN_FORMS = [
    ("SELECT a.k FROM a WHERE a.n = '2' AND a.n = 3", NONE, True),
    ("SELECT a.k FROM a WHERE a.n = '2'", "SELECT a.k FROM a WHERE a.n = 2", True),
    ("SELECT a.k FROM a WHERE '2' < a.n", "SELECT a.k FROM a WHERE a.n > 2", True),
    ("SELECT a.k FROM a WHERE a.n BETWEEN '1' AND '3'", "SELECT a.k FROM a WHERE a.n BETWEEN 1 AND 3", True),
    ("SELECT a.k FROM a WHERE a.n IN ('1', 2)", "SELECT a.k FROM a WHERE a.n IN (1, 2)", True),
    ("SELECT a.k FROM a WHERE a.n = '2'", "SELECT a.k FROM a WHERE a.n = 3", False),
]

@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right,same", FOLDED[:5] + COLUMN_FORMS[:5])
def test_bigquery_keeps_the_comparison_and_declines_it(prover, left, right, same):
    assert prover(left, right, dialect="bigquery", types=TYPES).status is SmtStatus.NOT_PROVEN

NOT_FOLDED = [
    "SELECT a.k FROM a WHERE '2.0' = 2",  # a decimal point: the engines convert it differently
    "SELECT a.k FROM a WHERE ' 2' = 2",  # a space
    "SELECT a.k FROM a WHERE '02' = 2",  # a leading zero
    "SELECT a.k FROM a WHERE '+2' = 2",
    "SELECT a.k FROM a WHERE '1234567890' = 1234567890",  # ten digits: MySQL compares through a float
    "SELECT a.k FROM a WHERE 'abc' = 0",
    "SELECT a.k FROM a WHERE a.x = '2'",  # a string column: the number reading does not apply
    "SELECT a.k FROM a WHERE a.z = '2'",  # a column with no declared type
    "SELECT a.k FROM a WHERE a.n IN ('1', 'x')",
    "SELECT a.k FROM a WHERE a.n IN ('1', a.k)",
    "SELECT a.k FROM a WHERE a.n <=> '2'",
]

@pytest.mark.parametrize("sql", NOT_FOLDED)
def test_other_strings_are_left_as_written(sql):
    assert string_number_literals.normalize(sql, "mysql", TYPES) == sql

def test_bigquery_and_other_dialects_are_left_as_written():
    sql = "SELECT a.k FROM a WHERE '2' = 2 AND a.n = '3'"
    for dialect in ("bigquery", "snowflake", "sqlite"):
        assert string_number_literals.normalize(sql, dialect, TYPES) == sql

def test_a_declared_float_or_decimal_column_is_left_as_written():
    sql = "SELECT a.k FROM a WHERE a.n = '2'"
    for declared in ("FLOAT", "DOUBLE", "DECIMAL(10, 2)", "VARCHAR"):
        assert string_number_literals.normalize(sql, "mysql", {"a": {"n": declared}}) == sql
    assert string_number_literals.normalize(sql, "mysql", {"a": {"n": "BIGINT"}}) != sql
