"""ANY/SOME/ALL subqueries and Calcite's expansions of them, checked against another engine on NULL-bearing data."""

from collections import Counter

import itertools

import random

import pytest

pytest.importorskip("z3")

import sqlglot

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.quantified_rules import fold_expansions, lower_quantified

SCHEMA = {"t": ["a", "b"], "u": ["c", "d"]}

def _bag(con, sql: str) -> Counter:
    return Counter(con.execute(sql).fetchall())

def _random_query(rng: random.Random) -> str:
    def subquery():
        where = rng.choice(["", " WHERE u.d > 1", " WHERE u.d = t.b", " WHERE u.c IS NOT NULL", " WHERE FALSE", " WHERE u.d <> t.a"])
        return f"SELECT {rng.choice(['u.c', 'u.d', 'u.c + 1', 'NULL'])} FROM u{where}"

    def condition(depth=2):
        if depth == 0 or rng.random() < 0.35:
            if rng.random() < 0.6:
                x = rng.choice(["t.a", "t.b", "1", "NULL", "t.a + t.b"])
                op = rng.choice(["=", "<>", "<", "<=", ">", ">="])
                return f"{x} {op} {rng.choice(['ANY', 'SOME', 'ALL'])} ({subquery()})"
            return rng.choice(["t.a > 1", "t.b IS NULL", "t.a = t.b"])
        op = rng.choice(["AND", "OR", "NOT"])
        if op == "NOT":
            return f"NOT ({condition(depth - 1)})"
        return f"({condition(depth - 1)}) {op} ({condition(depth - 1)})"

    shape = rng.random()
    if shape < 0.4:
        return f"SELECT t.a, t.b FROM t WHERE {condition()}"
    if shape < 0.7:
        return f"SELECT t.a, ({condition()}) AS v FROM t"
    if shape < 0.85:
        return f"SELECT t.a, CASE WHEN {condition()} THEN 1 ELSE 0 END AS v FROM t"
    return f"SELECT t.a, ({condition()}) IS NULL AS v FROM t"

def test_any_over_an_empty_subquery_is_false_and_all_is_true():
    left = "SELECT t.a FROM t WHERE t.a > ANY (SELECT u.c FROM u WHERE FALSE)"
    assert prove_equivalent_algebraic(left, "SELECT t.a FROM t WHERE FALSE", schema=SCHEMA).proven
    left = "SELECT t.a FROM t WHERE t.a > ALL (SELECT u.c FROM u WHERE FALSE)"
    assert prove_equivalent_algebraic(left, "SELECT t.a FROM t", schema=SCHEMA).proven

def test_not_any_is_all_of_the_negation():
    left = "SELECT t.a, NOT (t.a > ANY (SELECT u.c FROM u)) AS v FROM t"
    assert prove_equivalent_algebraic(left, "SELECT t.a, t.a <= ALL (SELECT u.c FROM u) AS v FROM t", schema=SCHEMA).proven

def test_any_and_all_are_not_confused():
    pairs = [
        ("SELECT t.a FROM t WHERE t.a > ANY (SELECT u.c FROM u)", "SELECT t.a FROM t WHERE t.a > ALL (SELECT u.c FROM u)"),
        ("SELECT t.a FROM t WHERE NOT t.a > ANY (SELECT u.c FROM u)", "SELECT t.a FROM t WHERE t.a < ALL (SELECT u.c FROM u)"),
        ("SELECT t.a, t.a > ANY (SELECT u.c FROM u) AS v FROM t", "SELECT t.a, t.a > ANY (SELECT u.c FROM u WHERE u.c IS NOT NULL) AS v FROM t"),
    ]
    for left, right in pairs:
        assert not prove_equivalent_algebraic(left, right, schema=SCHEMA).proven, (left, right)

_ANY_OR = (
    "((s.a {op} s.m) IS TRUE AND s.c <> 0) OR (s.c > s.ck AND NULL AND s.c <> 0 AND (s.a {op} s.m) IS NOT TRUE)"
    " OR ((s.a {op} s.m) AND s.c <> 0 AND (s.a {op} s.m) IS NOT TRUE AND s.c <= s.ck)"
)

_ANY_CASE = "CASE WHEN s.c = 0 THEN FALSE WHEN (s.a {op} s.m) IS TRUE THEN TRUE WHEN s.c > s.ck THEN NULL ELSE (s.a {op} s.m) END"

_IN_CASE = "CASE WHEN s.c = 0 THEN FALSE WHEN s.a IS NULL THEN NULL WHEN s.i IS NOT NULL THEN TRUE WHEN s.ck < s.c THEN NULL ELSE FALSE END"

_PROBE_CASE = "CASE WHEN s.n IS NULL THEN FALSE WHEN s.g = FALSE THEN NULL WHEN s.g IS NOT NULL THEN TRUE ELSE FALSE END"

_PROBE_MUTATIONS = [
    ("THEN NULL", "THEN FALSE"), ("(1 = u.c", "(2 = u.c"), ("IS NOT NULL) AS g", "IS NULL) AS g"), ("m.g DESC LIMIT", "m.g LIMIT"),
    ("OR u.c IS NULL", "OR u.d IS NULL"), ("LIMIT 1", "LIMIT 2"), ("s.n IS NULL THEN FALSE", "s.n IS NULL THEN NULL"),
]

_WHERES = ["", "WHERE u.d > 1"]

_MUTATIONS = [
    ("s.c <> 0", "s.c >= 0"), ("NULL AND", "FALSE AND"), ("s.c > s.ck", "s.c >= s.ck"), ("s.c <= s.ck", "s.c < s.ck"),
    ("IS NOT TRUE", "IS NOT FALSE"), ("THEN NULL", "THEN FALSE"), ("s.c = 0", "s.c = 1"), ("IS TRUE", "IS NOT FALSE"),
    ("s.a IS NULL", "s.b IS NULL"), ("ck < s.c", "ck <= s.c"),
]

def _wrap(condition: str, place: str, negate: bool) -> str:
    condition = f"NOT ({condition})" if negate else f"({condition})"
    return f"SELECT s.a, s.b FROM {{source}} WHERE {condition}" if place == "where" else f"SELECT s.a, {condition} AS v FROM {{source}}"

def _original(test: str, place: str, negate: bool) -> str:
    condition = f"NOT ({test})" if negate else f"({test})"
    return f"SELECT t.a, t.b FROM t WHERE {condition}" if place == "where" else f"SELECT t.a, {condition} AS v FROM t"

def _expansions():
    """(original, expansion, is a faithful expansion) triples, plus deliberately wrong variants."""

    cases = []
    for op, where, place, negate in itertools.product([">", "<="], _WHERES, ["where", "select"], [False, True]):
        agg = "MIN" if op in (">", ">=") else "MAX"
        original = _original(f"t.a {op} ANY (SELECT u.c FROM u {where})", place, negate)
        for form, wrong_agg in itertools.product((_ANY_OR, _ANY_CASE), (False, True)):
            used = ("MAX" if agg == "MIN" else "MIN") if wrong_agg else agg
            source = f"(SELECT t.a, t.b, g.m, g.c, g.ck FROM t INNER JOIN (SELECT {used}(u.c) AS m, COUNT(*) AS c, COUNT(u.c) AS ck FROM u {where}) AS g ON TRUE) AS s"
            expansion = _wrap(form.format(op=op), place, negate).format(source=source)
            cases.append((original, expansion, not wrong_agg))
            cases += [(original, expansion.replace(a, b, 1), False) for a, b in _MUTATIONS if a in expansion]
    for where, place, negate, group in itertools.product(_WHERES, ["where", "select"], [False, True], [" GROUP BY u.c", " GROUP BY u.c, TRUE", ""]):
        original = _original(f"t.a IN (SELECT u.c FROM u {where})", place, negate)
        grouped = bool(group)
        source = (
            f"(SELECT t.a, t.b, g.c, g.ck, ind.i FROM t INNER JOIN (SELECT COUNT(*) AS c, COUNT(u.c) AS ck FROM u {where}) AS g ON TRUE "
            f"LEFT JOIN (SELECT u.c AS k, TRUE AS i FROM u {where}{group}) AS ind ON t.a = ind.k) AS s"
        )
        expansion = _wrap(_IN_CASE, place, negate).format(source=source)
        cases.append((original, expansion, grouped))
        cases += [(original, expansion.replace(a, b, 1), False) for a, b in _MUTATIONS if a in expansion]
    # Calcite's constant IN: the first group of (y IS NOT NULL, COUNT(*)) over the rows equal to the constant or NULL.
    for where, place, negate in itertools.product(_WHERES, ["where", "select"], [False, True]):
        original = _original(f"1 IN (SELECT u.c FROM u {where})", place, negate)
        filtered = f"{where} AND" if where else "WHERE"
        source = (
            "(SELECT t.a, t.b, p.g, p.n FROM t LEFT JOIN (SELECT m.g, m.n FROM (SELECT f.g, COUNT(*) AS n FROM "
            f"(SELECT (u.c IS NOT NULL) AS g FROM u {filtered} (1 = u.c OR u.c IS NULL)) AS f GROUP BY f.g) AS m "
            "ORDER BY (m.g IS NULL) DESC, m.g DESC LIMIT 1) AS p ON TRUE) AS s"
        )
        expansion = _wrap(_PROBE_CASE, place, negate).format(source=source)
        cases.append((original, expansion, True))
        cases += [(original, expansion.replace(a, b, 1), False) for a, b in _PROBE_MUTATIONS if a in expansion]
    return cases

_LATERAL_WHERES = ["", "WHERE u.d > 1", "WHERE u.d = t.b"]

_PROBE_ONLY_CASE = "CASE WHEN s.g = FALSE THEN NULL WHEN s.g IS NOT NULL THEN TRUE ELSE FALSE END"

_LATERAL_MUTATIONS = [("WHERE t.a = k.k", "WHERE t.b = k.k"), ("LEFT JOIN LATERAL (SELECT k.k", "INNER JOIN LATERAL (SELECT k.k")]

def _lateral_expansions():
    cases = []
    for where, place, negate, group in itertools.product(_LATERAL_WHERES, ["where", "select"], [False, True], [" GROUP BY u.c", ""]):
        original = _original(f"t.a IN (SELECT u.c FROM u {where})", place, negate)
        source = (
            f"(SELECT t.a, t.b, g.c, g.ck, ind.i FROM t LEFT JOIN LATERAL (SELECT COUNT(*) AS c, COUNT(u.c) AS ck FROM u {where}) AS g ON TRUE "
            f"LEFT JOIN LATERAL (SELECT k.k, k.i FROM (SELECT u.c AS k, TRUE AS i FROM u {where}{group}) AS k WHERE t.a = k.k) AS ind ON TRUE) AS s"
        )
        expansion = _wrap(_IN_CASE, place, negate).format(source=source)
        cases.append((original, expansion, bool(group)))
        cases += [(original, expansion.replace(a, b, 1), False) for a, b in _MUTATIONS + _LATERAL_MUTATIONS if a in expansion]
    for where, place, negate, counted in itertools.product(_LATERAL_WHERES, ["where", "select"], [False, True], [True, False]):
        original = _original(f"1 IN (SELECT u.c FROM u {where})", place, negate)
        filtered = f"{where} AND" if where else "WHERE"
        n, count = (", p.n", ", COUNT(*) AS n") if counted else ("", "")
        source = (
            f"(SELECT t.a, t.b, p.g{n} FROM t LEFT JOIN LATERAL (SELECT m.g{n.replace('p.', ' m.') if counted else ''} FROM (SELECT f.g{count} FROM "
            f"(SELECT (u.c IS NOT NULL) AS g FROM u {filtered} (1 = u.c OR u.c IS NULL)) AS f GROUP BY f.g) AS m "
            "ORDER BY (m.g IS NULL) DESC, m.g DESC LIMIT 1) AS p ON TRUE) AS s"
        )
        expansion = _wrap(_PROBE_CASE if counted else _PROBE_ONLY_CASE, place, negate).format(source=source)
        cases.append((original, expansion, True))
        cases += [(original, expansion.replace(a, b, 1), False) for a, b in _PROBE_MUTATIONS if a in expansion]
    return cases

_CASES = _expansions() + _lateral_expansions()

def test_row_identity_keeps_table_arguments_and_deeper_readers():
    from kumo_bq_prover.ported.quantified_rules import _canonical

    sampled = _canonical(sqlglot.parse_one("SELECT r.b FROM (SELECT u.c AS b FROM u TABLESAMPLE SYSTEM (10 PERCENT)) AS r WHERE r.b > 0", read="bigquery"))
    assert "TABLESAMPLE" in sampled
    # r.b read from a nested select must not become u's own column b.
    nested = _canonical(sqlglot.parse_one("SELECT r.b FROM (SELECT u.c AS b FROM u) AS r WHERE EXISTS (SELECT 1 FROM w WHERE w.b = r.b)", read="bigquery"))
    assert nested != _canonical(sqlglot.parse_one("SELECT u.c FROM u WHERE EXISTS (SELECT 1 FROM w WHERE w.b = u.b)", read="bigquery"))
    assert "AS b" in nested

def test_a_stored_model_settles_a_fold_check_as_the_solver_would(monkeypatch):
    """``_same`` skips the solver when a model kept from an earlier check already tells the two readings apart."""

    import z3

    from kumo_bq_prover.ported import quantified_rules

    encoder = quantified_rules._Encoder({}, set())
    x = (z3.Bool("x_null"), z3.Real("x"))
    facts = quantified_rules._state_facts(encoder, False)
    condition = quantified_rules._quantified_semantics(encoder, x, ">", False)
    witnesses: list = []
    verdicts = []
    for quantifier_all in (False, True):
        for op in (">", ">=", "<", "<=", "=" if quantifier_all else "<>"):
            semantics = quantified_rules._quantified_semantics(encoder, x, op, quantifier_all)
            for mode in ("pos", "neg", "both"):
                alone = quantified_rules._same(encoder, condition, semantics, mode, facts)
                assert quantified_rules._same(encoder, condition, semantics, mode, facts, witnesses) == alone
                verdicts.append(alone)
    assert verdicts.count(True) == 3  # x > ANY (q) only reads the same as itself, in each mode
    assert 0 < len(witnesses) <= quantified_rules._WITNESSES

    solvers = []
    real = z3.Solver
    monkeypatch.setattr(z3, "Solver", lambda *args, **kwargs: solvers.append(1) or real(*args, **kwargs))
    semantics = quantified_rules._quantified_semantics(encoder, x, "<", True)
    assert not quantified_rules._same(encoder, condition, semantics, "both", facts, witnesses)
    assert solvers == []
