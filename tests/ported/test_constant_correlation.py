import sqlglot

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.constant_correlation import propagate_constant_correlations

TYPES = {"t": {"a": "int", "k": "int", "s": "varchar(20)"}, "u": {"k": "int", "x": "int", "s": "varchar(20)"}, "o": {"a": "int"}}

SCHEMA = {name: list(columns) for name, columns in TYPES.items()}

def _proven(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="mysql", compare_names=False).proven

def _rule(sql):
    out = propagate_constant_correlations(sqlglot.parse_one(sql, read="mysql"), TYPES)
    return out.sql(dialect="mysql") if out is not None else None

def test_pinned_column_of_derived_table_is_read_as_the_constant_inside_exists():
    left = "SELECT d.a FROM (SELECT t.a AS a, t.k AS k FROM t WHERE t.k = 200) AS d WHERE EXISTS (SELECT 1 FROM u WHERE d.k = u.k)"
    right = "SELECT d.a FROM (SELECT t.a AS a, t.k AS k FROM t WHERE t.k = 200) AS d WHERE EXISTS (SELECT 1 FROM u WHERE 200 = u.k)"
    assert _proven(left, right)

def test_pinned_conjunct_of_the_same_where_reaches_exists_and_in():
    assert _rule("SELECT t.a FROM t WHERE t.k = 7 AND EXISTS (SELECT 1 FROM u WHERE u.k = t.k)") == (
        "SELECT t.a FROM t WHERE t.k = 7 AND EXISTS(SELECT 1 FROM u WHERE u.k = 7)"
    )
    left = "SELECT t.a FROM t WHERE t.k = 7 AND t.a IN (SELECT u.x FROM u WHERE u.k > t.k)"
    right = "SELECT t.a FROM t WHERE t.k = 7 AND t.a IN (SELECT u.x FROM u WHERE u.k > 7)"
    assert _rule(left) == sqlglot.parse_one(right, read="mysql").sql(dialect="mysql")
    assert _proven(left, right)

def test_non_integer_shadowed_and_aggregated_references_are_left_alone():
    # a string column pinned to an integer literal may hold '200abc' under MySQL's coercion
    assert _rule("SELECT t.a FROM t WHERE t.s = 200 AND EXISTS (SELECT 1 FROM u WHERE u.k = t.s)") is None
    # the other operand is a string: the comparison's coercion could differ
    assert _rule("SELECT t.a FROM t WHERE t.k = 200 AND EXISTS (SELECT 1 FROM u WHERE u.s = t.k)") is None
    # t inside the subquery is the subquery's own t
    assert _rule("SELECT t.a FROM t WHERE t.k = 200 AND EXISTS (SELECT 1 FROM u AS t WHERE t.x = t.k)") is None
    # an aggregate over an empty filter still returns a row, with a NULL k
    assert _rule("SELECT d.a FROM (SELECT t.k AS k, COUNT(*) AS a FROM t WHERE t.k = 200) AS d WHERE EXISTS (SELECT 1 FROM u WHERE u.k = d.k)") is None
    # only references inside a subquery are rewritten
    assert _rule("SELECT t.a FROM t WHERE t.k = 200 AND t.a > t.k") is None
