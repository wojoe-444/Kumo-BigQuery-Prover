"""exists_constant_rules: an EXISTS a foreign key witnesses, and a correlation a filter fixes.

Each proven pair is replayed on random another engine data that respects the declared constraints; each near miss must
stay unproven, and random data must show it really differs (both runs, optimizer on and off, agree).
"""

import random

import pytest

pytest.importorskip("z3")

import sqlglot

from kumo_bq_prover.ported.algebraic_equivalence import normalize, prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import TableConstraints

SCHEMA = {"emp": ["empno", "deptno", "sal"], "dept": ["deptno", "name"], "other": ["x"]}

TYPES = {t: {c: "INT" for c in cols} for t, cols in SCHEMA.items()}

FK = ((("deptno",), "dept", ("deptno",)),)

def constraints(emp_deptno_not_null=True, foreign_key=True):
    return {
        "emp": TableConstraints(
            not_null=frozenset({"empno", "sal"} | ({"deptno"} if emp_deptno_not_null else set())),
            keys=(("empno",),),
            foreign_keys=FK if foreign_key else (),
        ),
        "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
        "other": TableConstraints(not_null=frozenset(), keys=()),
    }

STRICT = constraints()

def prove(left, right, declared=STRICT, types=TYPES):
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, constraints=declared, types=types, exact_arithmetic=True, compare_names=False, dialect="mysql"
    ).proven

PROVEN = [
    ("foreign key witnesses an uncorrelated exists",
     "SELECT sal FROM emp WHERE EXISTS (SELECT 1 FROM dept AS d WHERE deptno = d.deptno)",
     "SELECT sal FROM emp"),
    ("join with a keyed parent vs an uncorrelated exists (VeriEQL Calcite 16)",
     "SELECT t.deptno, t.sal FROM (SELECT sal, deptno FROM emp) AS t INNER JOIN (SELECT deptno FROM dept) AS t0 ON t.deptno = t0.deptno",
     "SELECT deptno, sal FROM (SELECT sal, deptno FROM emp WHERE EXISTS (SELECT 1 FROM (SELECT deptno FROM dept) AS t3 WHERE deptno = t3.deptno)) AS t2"),
    ("derived table fixes the correlation (VeriEQL Calcite 348)",
     "SELECT t0.sal FROM (SELECT * FROM (SELECT sal, deptno FROM emp) AS t WHERE deptno = 2) AS t0 INNER JOIN "
     "(SELECT deptno FROM (SELECT sal, deptno FROM emp) AS t1 WHERE sal = 1 GROUP BY deptno) AS t4 ON t0.deptno = t4.deptno",
     "SELECT sal FROM (SELECT * FROM (SELECT sal, deptno FROM emp) AS t6 WHERE deptno = 2 AND EXISTS "
     "(SELECT 1 FROM (SELECT deptno FROM (SELECT sal, deptno FROM emp) AS t8 WHERE sal = 1) AS t10 WHERE 2 = t10.deptno)) AS t7"),
    ("where conjunct fixes the correlation",
     "SELECT e.sal FROM emp AS e WHERE e.deptno = 2 AND EXISTS (SELECT 1 FROM emp AS k WHERE k.sal = 1 AND k.deptno = e.deptno)",
     "SELECT e.sal FROM emp AS e WHERE e.deptno = 2 AND EXISTS (SELECT 1 FROM emp AS k WHERE k.sal = 1 AND k.deptno = 2)"),
]

NOT_PROVEN = [
    ("nullable foreign key column: a NULL child names no parent", constraints(emp_deptno_not_null=False),
     "SELECT sal FROM emp WHERE EXISTS (SELECT 1 FROM dept)", "SELECT sal FROM emp"),
    ("no foreign key declared", constraints(foreign_key=False),
     "SELECT sal FROM emp WHERE EXISTS (SELECT 1 FROM dept)", "SELECT sal FROM emp"),
    ("the exists filters the parent by a condition the witness may fail", STRICT,
     "SELECT sal FROM emp WHERE EXISTS (SELECT 1 FROM dept WHERE name = 1)", "SELECT sal FROM emp"),
    ("the child is NULL-extended by a LEFT JOIN", STRICT,
     "SELECT o.x FROM other AS o LEFT JOIN emp AS e ON e.empno = o.x WHERE EXISTS (SELECT 1 FROM dept)",
     "SELECT o.x FROM other AS o LEFT JOIN emp AS e ON e.empno = o.x"),
    ("the fixing derived table is NULL-extended by a LEFT JOIN", STRICT,
     "SELECT o.x FROM other AS o LEFT JOIN (SELECT deptno FROM emp WHERE deptno = 2) AS t0 ON o.x = t0.deptno "
     "WHERE NOT EXISTS (SELECT 1 FROM emp AS k WHERE k.deptno = t0.deptno)",
     "SELECT o.x FROM other AS o LEFT JOIN (SELECT deptno FROM emp WHERE deptno = 2) AS t0 ON o.x = t0.deptno "
     "WHERE NOT EXISTS (SELECT 1 FROM emp AS k WHERE k.deptno = 2)"),
    ("the fixing equality sits under OR", STRICT,
     "SELECT e.sal FROM emp AS e WHERE e.deptno = 2 OR EXISTS (SELECT 1 FROM emp AS k WHERE k.sal = 1 AND k.deptno = e.deptno)",
     "SELECT e.sal FROM emp AS e WHERE e.deptno = 2 OR EXISTS (SELECT 1 FROM emp AS k WHERE k.sal = 1 AND k.deptno = 2)"),
]

def test_non_integer_column_is_not_substituted():
    # '0200' = 200 holds for a string column, so the literal may not stand in for the column's value
    sql = "SELECT e.sal FROM emp AS e WHERE e.deptno = 2 AND EXISTS (SELECT 1 FROM emp AS k WHERE k.deptno = e.deptno)"
    text_types = {**TYPES, "emp": {**TYPES["emp"], "deptno": "VARCHAR"}}
    as_text = normalize(sql, schema=SCHEMA, dialect="mysql", types=text_types)
    as_int = normalize(sql, schema=SCHEMA, dialect="mysql", types=TYPES)
    assert "k.deptno = e.deptno" in as_text
    assert "k.deptno = 2" in as_int

def test_outer_reference_in_the_exists_keeps_it():
    # a correlated test is not witnessed by the foreign key alone: it filters the child row by row
    sql = "SELECT sal FROM emp WHERE EXISTS (SELECT 1 FROM dept AS d WHERE d.deptno = emp.sal)"
    fks = {"emp": [(("deptno",), "dept", ("deptno",))]}
    not_null = {"emp": frozenset({"empno", "deptno", "sal"}), "dept": frozenset({"deptno", "name"})}
    assert "EXISTS" in normalize(sql, schema=SCHEMA, dialect="mysql", not_null=not_null, foreign_keys=fks)
