"""A string compared with a number is not "never equal".

``WHERE '2' <> 2`` was proven equal to the query without the filter because the provers call values of different kinds
unequal. MySQL compares a string with a number as numbers, another engine and PostgreSQL cast the string, and BigQuery rejects the
comparison, so no engine returns every row. The provers decline such pairs; near misses that compare two numbers or two
strings stay proven. See ``kumo_bq_prover.ported.string_number_compare``.
"""

from collections import Counter

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported import string_number_compare

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import prove_equivalent_smt, SmtStatus

PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)

BIGQUERY_DECLINED = [
    ("SELECT t.a FROM t WHERE '2' <> 2", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' = 2", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' < 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE 2 <> '2'", "SELECT t.a FROM t"),
]

DECLINED = [
    ("SELECT t.a FROM t WHERE '2' BETWEEN 1 AND 3", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE '2' IN (2, 3)", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE WHEN '2.5' = 2 THEN 1 ELSE 0 END = 0", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE CASE '2' WHEN 2 THEN FALSE ELSE TRUE END", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE NULLIF('2', 2) IS NULL", "SELECT t.a FROM t WHERE FALSE"),
    # the same column against a string and a number: 'abc' reads as 0 on MySQL, so a = 0 satisfies both
    ("SELECT t.a FROM t WHERE t.a = 'abc' AND t.a = 0", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE t.a = '2.0' AND t.a = 2", "SELECT t.a FROM t WHERE FALSE"),
]

PROVEN = [
    ("SELECT t.a FROM t WHERE 2 <> 2", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' <> '2'", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE '2' = '3'", "SELECT t.a FROM t WHERE FALSE"),
    ("SELECT t.a FROM t WHERE 2 = 2", "SELECT t.a FROM t"),
    ("SELECT t.a FROM t WHERE t.a = 1 AND t.a = 2", "SELECT t.a FROM t WHERE FALSE"),
]

TEXT_ONLY = [("SELECT t.a FROM t WHERE t.a = 'x' AND t.a = 'y'", "SELECT t.a FROM t WHERE FALSE")]

@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", BIGQUERY_DECLINED + [("SELECT t.a FROM t WHERE CASE WHEN '2' = 2 THEN 1 ELSE 0 END = 0", "SELECT t.a FROM t")])
def test_bigquery_keeps_declining_a_string_against_a_number(prover, left, right):
    result = prover(left, right, dialect="bigquery")
    assert result.status is SmtStatus.NOT_PROVEN, (left, result.reason)
