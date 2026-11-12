"""Volatile calls in every dialect (``kumo_bq_prover.ported.volatile_calls``).

A function the provers have no rule for is read as a function of its arguments, so ``F() = F()`` is
``F() IS NOT NULL``. Until 2026-11-11 each prover kept a short list of exceptions written for BigQuery, and
``nextval('q')``, ``clock_timestamp()``, ``randomblob(4)``, ``uuid_short()`` and ``sysdate(6)`` were proved that way.
In PostgreSQL ``WHERE nextval('q') = nextval('q')`` returns no row and ``WHERE nextval('q') IS NOT NULL`` every row.
"""

from __future__ import annotations

import pytest

import sqlglot

from kumo_bq_prover.ported import volatile_calls as V

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.equivalence import prove_equivalent

from kumo_bq_prover.ported.smt_equivalence import UNLISTED_FUNCTION_ASSUMPTION, SmtStatus, prove_equivalent_smt

z3 = pytest.importorskip("z3")

SCHEMA = {"t1": ["s1", "s2"]}

def _cases(table):
    return [(dialect, call) for dialect, calls in table.items() for call in calls]

def _tree(dialect, call):
    return sqlglot.parse_one(f"select {call} from t", read=dialect)

def test_a_clock_string_counts_only_where_it_is_read_as_a_time():
    assert V.contains(_tree("sqlite", "date('now')"), "sqlite")
    assert V.contains(_tree("bigquery", "cast('now' as timestamp)"), "bigquery")
    assert not V.contains(_tree("bigquery", "a = 'now'"), "bigquery")
    assert V.contains(_tree("postgres", "a = 'now'"), "postgres")  # the literal takes the type of a, which may be a timestamp
    assert not V.contains(_tree("postgres", "a = 'noon'"), "postgres")

def test_a_keyword_value_is_a_column_where_the_dialect_says_so():
    assert not V.contains(_tree("bigquery", "user"), "bigquery")
    assert V.contains(_tree("postgres", "user"), "postgres")
    assert not V.contains(_tree("postgres", '"user"'), "postgres")
    assert V.contains(_tree("oracle", "rownum"), "oracle") and not V.contains(_tree("bigquery", "rownum"), "bigquery")
    assert V.contains(_tree("snowflake", "seq.nextval"), "snowflake") and not V.contains(_tree("snowflake", "nextval"), "snowflake")

def test_unlisted_calls_are_named():
    tree = sqlglot.parse_one("select my_udf(a), ds.other(b), upper(c), nextval('q') from t", read="bigquery")
    assert V.unlisted_calls(tree) == ("DS.OTHER", "MY_UDF")
    assert V.read("select my_udf(a) from t", "bigquery") == (None, ("MY_UDF",))
    assert V.read("select rand(", "bigquery") == (None, ())  # unreadable text is the prover's to report
    why, names = V.read("select my_udf(a), now() from t", "mysql")
    assert "NOW()" in why.upper() and names == ()

def test_the_refusal_names_the_call():
    result = prove_equivalent_algebraic(
        "select s1 from t1 where nextval('q') = nextval('q')", "select s1 from t1 where nextval('q') is not null", schema=SCHEMA, dialect="postgres"
    )
    assert "nondeterministic" in result.reason and "NEXTVAL" in result.reason.upper()
    result = prove_equivalent_smt("select s1 from t1 where s2 > date('now')", "select s1 from t1 where s2 > date('now')", schema=SCHEMA, dialect="sqlite")
    assert "reads the clock or the session" in result.reason

@pytest.mark.parametrize(
    "left,right",
    [
        # The rules drop or fold the call before the solver sees the statement; the pair as written still holds it.
        ("select s1 from t1 where rand() < 2 or true", "select s1 from t1"),
        ("select s1 from t1 where case when true then 1 else rand() end = 1", "select s1 from t1"),
    ],
)
def test_a_call_the_rules_removed_still_blocks_the_proof(left, right):
    for prover in (prove_equivalent_smt, prove_equivalent_algebraic):
        result = prover(left, right, schema=SCHEMA)
        assert result.status is SmtStatus.NOT_PROVEN and "nondeterministic" in result.reason, (prover.__name__, result.reason)

def test_a_function_the_prover_does_not_know_is_an_assumption_of_the_proof():
    left = "select s1 from t1 where my_udf(s1) = my_udf(s1)"
    right = "select s1 from t1 where my_udf(s1) is not null"
    prefix = UNLISTED_FUNCTION_ASSUMPTION.split("{")[0]
    for prover in (prove_equivalent_smt, prove_equivalent_algebraic):
        result = prover(left, right, schema=SCHEMA)
        assert result.proven, result.reason
        assert [a for a in result.assumptions if a.startswith(prefix)] == [prefix + "MY_UDF"], result.assumptions
    plain = prove_equivalent_algebraic("select s1 from t1 where upper(s1) = 'A'", "select s1 from t1 where 'A' = upper(s1)", schema=SCHEMA)
    assert plain.proven and not any(a.startswith(prefix) for a in plain.assumptions)

def test_a_proof_without_such_calls_is_unchanged():
    result = prove_equivalent_algebraic("select s1 from t1 where s2 = 1 and s1 = 2", "select s1 from t1 where s1 = 2 and s2 = 1", schema=SCHEMA, dialect="mysql")
    assert result.proven

@pytest.mark.parametrize("call", ["RAND()", "CURRENT_TIMESTAMP()", "NEXTVAL('q')", "CAST('now' AS TIMESTAMP)", "KEYS.NEW_KEYSET('AEAD_AES_GCM_256')"])
def test_the_structural_prover_keeps_an_untouched_call(call):
    sql = f"SELECT a FROM t WHERE b = {call}"
    assert prove_equivalent(sql, f"SELECT a FROM t WHERE (b = {call})").proven

@pytest.mark.parametrize("call", ["NEXTVAL('q')", "CAST('now' AS TIMESTAMP)", "CLOCK_TIMESTAMP()"])
def test_the_structural_prover_refuses_a_call_that_moved_or_changed(call):
    assert not prove_equivalent(f"SELECT a FROM t WHERE {call} = {call}", f"SELECT a FROM t WHERE {call} IS NOT NULL").proven
    assert not prove_equivalent(f"SELECT a, {call} AS v FROM t", f"SELECT a, v FROM (SELECT a, {call} AS v FROM t)").proven
