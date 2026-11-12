"""Queries that read the statement time or the session (``kumo_bq_prover.ported.statement_values``).

``NOW()``, ``CURRENT_DATE`` and ``SESSION_USER()`` have one value for the whole statement, so two queries that read
them can be equal *when both read the same value*. The solver-based provers say so as ``PROVEN_CONDITIONALLY`` with
a condition naming the calls; they never call such a pair ``PROVEN_EQUIVALENT``. A call that may differ within one
statement (``RAND()``, ``nextval``, ``clock_timestamp()``) is still not proven at all.
"""

from __future__ import annotations

import pytest

from kumo_bq_prover.ported import statement_values as S

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import UNLISTED_FUNCTION_ASSUMPTION, Counterexample, SmtEquivalenceResult, SmtStatus, prove_equivalent_smt

z3 = pytest.importorskip("z3")

SCHEMA = {"t": ["a", "d", "u"], "s": ["a", "d"]}

TYPES = {"t": {"a": "INT64", "d": "DATE", "u": "STRING"}, "s": {"a": "INT64", "d": "DATE"}}

def prove(prover, left, right, dialect="bigquery", **options):
    return prover(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, **options)

def no_stand_in(result) -> bool:
    texts = [result.reason, *result.assumptions, *(c.text for c in result.conditions), *(n for c in result.conditions for n in c.columns)]
    return not any(S.PREFIX.lower() in text.lower() for text in texts)

def test_the_clock_and_the_session_are_two_conditions():
    left = "select a from t where d < current_date and u = current_user"
    right = "select a from t where current_user = u and current_date > d"
    result = prove(prove_equivalent_algebraic, left, right, "postgres")
    assert [(c.kind, c.columns) for c in result.conditions] == [(S.SAME_SESSION, ("CURRENT_USER",)), (S.SAME_TIME, ("CURRENT_DATE",))]
    assert "in the same session and at the same statement time" in result.reason

def test_a_function_the_prover_does_not_know_is_still_named_and_the_stand_in_is_not():
    left = "select a, my_udf(a) from t where d < current_date()"
    right = "select a, my_udf(a) from t where current_date() > d"
    result = prove(prove_equivalent_smt, left, right)
    assert result.status is SmtStatus.PROVEN_CONDITIONALLY
    prefix = UNLISTED_FUNCTION_ASSUMPTION.split("{")[0]
    assert [a for a in result.assumptions if a.startswith(prefix)] == [prefix + "MY_UDF"]
    alone = prove(prove_equivalent_smt, "select a from t where d < current_date()", "select a from t where current_date() > d")
    assert not any(a.startswith(prefix) for a in alone.assumptions)

NOT_THE_SAME = [
    ("bigquery", "select a from t where d < current_date()", "select a from t where d <= current_date()"),
    ("bigquery", "select a from t where current_date() > date '2020-01-01'", "select a from t"),  # true today; not a fact
    ("bigquery", "select a from t where d < current_date()", "select a from t where d < current_date('UTC')"),
    ("bigquery", "select a from t where d < current_date()", "select a from t where d < date(current_timestamp())"),
    ("bigquery", "select a from t where d < current_date()", "select a from t where d < my_udf()"),
    ("postgres", "select a from t where d < now()", "select a from t where d < statement_timestamp()"),  # two clocks in a transaction
    ("mysql", "select a from t where d < now()", "select a from t where d < now(6)"),
    ("mysql", "select a from t where u = user()", "select a from t where u = current_user()"),  # the client's name and the account's
]

LEFT_OUT = [
    # SQLite promises one value only within one step; the others were not checked
    ("sqlite", "select a from t where d < current_timestamp"),
    ("sqlite", "select a from t where d < date('now')"),
    ("tsql", "select a from t where d < getdate()"),
    ("snowflake", "select a from t where d < current_timestamp()"),
    ("oracle", "select a from t where d < sysdate"),
    ("clickhouse", "select a from t where d < now()"),
    # read by the engine, not written as a call
    ("postgres", "select a from t where d < 'now'"),
    ("postgres", "select a from t where d < 'today'::date"),
    ("postgres", "select a from t where u = user"),
    ("postgres", "select a from t where age(d) > interval '1 day'"),
    ("bigquery", "select a from t where u = session_user"),
    # not a value of the statement alone
    ("mysql", "select a from t where a < last_insert_id()"),
    ("mysql", "select a from t where a < found_rows()"),
]

def test_everything_but_the_call_stays_as_it_was_written():
    left = "select a from t where d < NOW( 6 ) and x = `now` and u = 'now()' -- now()\n and curdate() > d"
    found = S.reading(left, "select 1", "mysql")
    assert found is not None
    names = {call: name for name, _, call in found.names}
    assert found.left_sql == left.replace("NOW( 6 )", names["CURRENT_TIMESTAMP(6)"] + "()").replace("curdate()", names["CURRENT_DATE"] + "()")
    assert found.right_sql == "select 1"
    assert found.calls == {S.SAME_TIME: ("CURRENT_DATE", "CURRENT_TIMESTAMP(6)")}
    assert found.restore(f"{names['CURRENT_DATE']}() is NULL") == "CURRENT_DATE is NULL"

@pytest.mark.parametrize(
    "dialect,left",
    [
        ("bigquery", "select a as current_date from t where d < current_date"),  # an alias the scan cannot tell from the call
        ("bigquery", "select a from t where d < current_date; select 1"),  # two statements
        ("bigquery", "select a from t where d < kq_fixed_1_current_date() and d < current_date"),  # a stand-in name of its own
        ("bigquery", "select a from t where d < current_date("),  # not SQL
    ],
)
def test_a_text_the_scan_and_the_parser_read_differently_is_left_alone(dialect, left):
    assert S.reading(left, "select a from t", dialect) is None
    assert not prove(prove_equivalent_algebraic, left, left, dialect).proven

def test_a_column_or_a_function_of_that_name_is_not_the_call():
    found = S.reading("select t.current_date from t where d < current_date", "select a from t", "bigquery")
    assert found.left_sql == f"select t.current_date from t where d < {found.names[0][0]}()"
    # mydb.now() is somebody's own function: an unknown function like any other, named in the assumptions
    sql = "select a from t where d < mydb.now()"
    assert S.reading(sql, sql, "mysql") is None
    result = prove(prove_equivalent_smt, sql, sql, "mysql")
    assert result.proven and any("MYDB.NOW" in a for a in result.assumptions)

def test_a_text_without_such_a_call_is_not_parsed_here():
    assert S.reading("select a from t where d < date '2024-01-01'", "select nowhere, currently from t", "bigquery") is None
    result = prove(prove_equivalent_algebraic, "select a from t where a = 1 and d is null", "select a from t where d is null and a = 1")
    assert result.proven and not result.conditions

def test_a_difference_is_not_reported_as_a_counterexample():
    calls = []

    def refuting(left_sql, right_sql, **kwargs):
        calls.append((left_sql, right_sql))
        example = Counterexample({"t": [{"a": 1}]}, [(1,)], [])
        return SmtEquivalenceResult(SmtStatus.NOT_EQUIVALENT, "the queries differ", counterexample=example)

    left, right = "select a from t where d < current_date()", "select a from t where d <= current_date()"
    result = S.proved(refuting, left, right, {"dialect": "bigquery"})
    assert S.PREFIX in calls[0][0] and S.PREFIX in calls[0][1]  # the prover saw the stand-ins, not the calls
    assert result.status is SmtStatus.NOT_PROVEN and result.counterexample is None
    assert "CURRENT_DATE" in result.reason and "when each query runs" in result.reason
    # a pair with no such call keeps its refutation
    plain = S.proved(refuting, "select a from t", "select a from t where a = 1", {"dialect": "bigquery"})
    assert plain.status is SmtStatus.NOT_EQUIVALENT and plain.counterexample is not None
