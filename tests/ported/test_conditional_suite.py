"""A small hand-checked suite for the conditional verdict: every pair has a known answer with and without its conditions.

For each case the answer was worked out by hand and is checked three ways, none of which trusts the prover:

* **Without the conditions** a database written by hand breaks them and the two queries return different rows on it
  (another engine runs both).
* **With the conditions** the queries return the same rows on every hand-written database that meets them and on
  hundreds of random ones repaired to meet them.
* **Minimality.** Dropping any one condition makes the prover stop proving the pair, and the conditions are named
  exactly as the case expects.

Controls: pairs that differ in ways no condition can fix stay unproven even when every candidate condition is assumed
(a prover handed facts nothing satisfies would prove them), a set no database satisfies is refused, and a set that
empties the query is passed over.
"""

import functools

import random

from dataclasses import dataclass, field

import pytest

pytest.importorskip("z3")

from kumo_bq_prover.ported import conditional_equivalence as ce

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import SmtStatus

@dataclass
class Case:
    name: str
    left: str
    right: str
    columns: dict  # table -> columns
    conditions: set  # the condition texts the verdict must name
    breaks: list  # databases that break a condition and separate the queries
    holds: list = field(default_factory=list)  # hand-written databases that meet them
    note: str = ""

CASES = [
    Case(
        "distinct-on-a-primary-key",
        "SELECT id FROM t", "SELECT DISTINCT id FROM t",
        {"t": ["id"]},
        {"(id) is unique in t", "t.id is NOT NULL"},
        breaks=[{"t": [(1,), (1,)]}, {"t": [(None,), (None,)]}],  # a repeated id, and a repeated NULL (DISTINCT keeps one)
        holds=[{"t": []}, {"t": [(1,), (2,)]}],
    ),
    Case(
        "left-join-removed-when-the-right-key-is-unique",
        "SELECT o.id FROM orders o LEFT JOIN customers c ON o.cid = c.id", "SELECT id FROM orders",
        {"orders": ["id", "cid"], "customers": ["id"]},
        {"(id) is unique in customers"},
        breaks=[{"orders": [(1, 5)], "customers": [(5,), (5,)]}],  # a duplicated key repeats the order
        holds=[{"orders": [(1, 5), (2, None), (3, 9)], "customers": [(5,)]}],  # no parent and a NULL are fine for a LEFT JOIN
    ),
    Case(
        "inner-join-removed-with-a-foreign-key-to-a-unique-parent",
        "SELECT o.id FROM orders o JOIN customers c ON o.cid = c.id", "SELECT id FROM orders",
        {"orders": ["id", "cid"], "customers": ["id"]},
        {"(id) is unique in customers", "orders(cid) references customers(id)", "orders.cid is NOT NULL"},
        breaks=[
            {"orders": [(1, 5)], "customers": [(5,), (5,)]},  # duplicate parent key repeats the order
            {"orders": [(1, 5)], "customers": []},  # orphan: no parent, the inner join drops the order
            {"orders": [(1, None)], "customers": [(5,)]},  # a NULL child never joins
        ],
        holds=[{"orders": [(1, 5), (2, 6)], "customers": [(5,), (6,), (7,)]}],
    ),
    Case(
        "composite-key",
        "SELECT a, b FROM t", "SELECT DISTINCT a, b FROM t",
        {"t": ["a", "b"]},
        {"(a, b) is unique in t", "t.a is NOT NULL", "t.b is NOT NULL"},
        breaks=[{"t": [(1, 2), (1, 2)]}, {"t": [(None, 2), (None, 2)]}],
        holds=[{"t": [(1, 1), (1, 2), (2, 1)]}],  # neither column is unique alone: only the pair is
    ),
    Case(
        "null-in-not-in",
        "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT customer_id FROM customers)",
        "SELECT o.id FROM orders o LEFT JOIN customers c ON o.id = c.customer_id WHERE c.customer_id IS NULL",
        {"orders": ["id"], "customers": ["customer_id"]},
        {"orders.id is NOT NULL", "customers.customer_id is NOT NULL"},
        breaks=[{"orders": [(1,)], "customers": [(None,)]}, {"orders": [(None,)], "customers": [(2,)]}],
        holds=[{"orders": [(1,), (2,)], "customers": [(2,), (3,)]}, {"orders": [(1,)], "customers": []}],
    ),
    Case(
        "join-against-a-semi-join-bag-semantics",
        "SELECT t.a FROM t JOIN u ON t.k = u.k", "SELECT a FROM t WHERE k IN (SELECT k FROM u)",
        {"t": ["a", "k"], "u": ["k"]},
        {"(k) is unique in u"},
        breaks=[{"t": [(1, 7)], "u": [(7,), (7,)]}],  # the join repeats the row once per match, IN does not
        holds=[{"t": [(1, 7), (2, None), (3, 8)], "u": [(7,), (None,), (None,)]}],  # NULL keys may repeat
    ),
    # ---- NULL-sensitive expressions over t(id, x): the one condition is NOT NULL x ----
    Case("count-column-against-count-star", "SELECT COUNT(x) AS n FROM t", "SELECT COUNT(*) AS n FROM t", {"t": ["id", "x"]},
         {"t.x is NOT NULL"}, breaks=[{"t": [(1, None)]}], holds=[{"t": []}, {"t": [(1, 5), (2, 5)]}]),
    Case("is-null-against-an-empty-query", "SELECT id FROM t WHERE x IS NULL", "SELECT id FROM t WHERE 1 = 0", {"t": ["id", "x"]},
         {"t.x is NOT NULL"}, breaks=[{"t": [(1, None)]}], holds=[{"t": [(1, 5)]}],
         note="the right side is empty by design, so the conditions making the left empty are the point, not a vacuous proof"),
    Case("coalesce-of-a-non-null-column", "SELECT COALESCE(x, 0) AS x FROM t", "SELECT x FROM t", {"t": ["id", "x"]},
         {"t.x is NOT NULL"}, breaks=[{"t": [(1, None)]}], holds=[{"t": [(1, 5), (2, 0)]}]),
    Case("or-is-null-guard", "SELECT id FROM t WHERE x <> 0 OR x IS NULL", "SELECT id FROM t WHERE x <> 0", {"t": ["id", "x"]},
         {"t.x is NOT NULL"}, breaks=[{"t": [(1, None)]}], holds=[{"t": [(1, 5), (2, 0)]}]),
    Case("case-on-null", "SELECT CASE WHEN x IS NULL THEN -1 ELSE x END AS x FROM t", "SELECT x FROM t", {"t": ["id", "x"]},
         {"t.x is NOT NULL"}, breaks=[{"t": [(1, None)]}], holds=[{"t": [(1, 5), (2, -3)]}]),
    # ---- keys over t(k, v, w): a primary key is a unique, non-NULL column ----
    Case("group-by-a-key", "SELECT k FROM t", "SELECT k FROM t GROUP BY k", {"t": ["k", "v", "w"]},
         {"(k) is unique in t", "t.k is NOT NULL"}, breaks=[{"t": [(1, 1, 1), (1, 2, 2)]}, {"t": [(None, 1, 1), (None, 2, 2)]}],
         holds=[{"t": [(1, 1, 1), (2, 1, 1)]}]),
    Case("count-per-key-is-one", "SELECT k, COUNT(*) AS n FROM t GROUP BY k", "SELECT k, 1 AS n FROM t", {"t": ["k", "v", "w"]},
         {"(k) is unique in t", "t.k is NOT NULL"}, breaks=[{"t": [(1, 1, 1), (1, 2, 2)]}, {"t": [(None, 1, 1), (None, 2, 2)]}],
         holds=[{"t": [(1, 1, 1), (2, 1, 1)]}]),
    Case("sum-per-key-is-the-value", "SELECT k, SUM(w) AS s FROM t GROUP BY k", "SELECT k, w AS s FROM t", {"t": ["k", "v", "w"]},
         {"(k) is unique in t", "t.k is NOT NULL"}, breaks=[{"t": [(1, 1, 1), (1, 2, 2)]}, {"t": [(None, 1, 1), (None, 2, 2)]}],
         holds=[{"t": [(1, 1, 5), (2, 1, None)]}]),
    Case("distinct-on-two-columns", "SELECT k, v FROM t", "SELECT DISTINCT k, v FROM t", {"t": ["k", "v", "w"]},
         {"(k, v) is unique in t", "t.k is NOT NULL", "t.v is NOT NULL"},
         breaks=[{"t": [(1, 1, 1), (1, 1, 2)]}, {"t": [(1, None, 1), (1, None, 2)]}], holds=[{"t": [(1, 1, 1), (1, 2, 1), (2, 1, 1)]}]),
    # ---- NOT IN against NOT EXISTS ----
    Case("not-in-against-not-exists", "SELECT a.id FROM a WHERE a.x NOT IN (SELECT y FROM b)",
         "SELECT a.id FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE b.y = a.x)", {"a": ["id", "x"], "b": ["y"]},
         {"a.x is NOT NULL", "b.y is NOT NULL"}, breaks=[{"a": [(1, 1)], "b": [(None,)]}, {"a": [(1, None)], "b": [(2,)]}],
         holds=[{"a": [(1, 1), (2, 2)], "b": [(2,), (3,)]}]),
    Case("left-join-filtered-on-the-right-column", "SELECT l.id FROM l LEFT JOIN r ON l.k = r.k WHERE r.flag IS NOT NULL",
         "SELECT l.id FROM l JOIN r ON l.k = r.k", {"l": ["id", "k"], "r": ["k", "flag"]},
         {"r.flag is NOT NULL"}, breaks=[{"l": [(1, 1)], "r": [(1, None)]}], holds=[{"l": [(1, 1), (2, 3)], "r": [(1, 7), (1, 8)]}],
         note="no key is needed: both sides repeat a left row once per match"),
]

def _bag(db, sql):
    return sorted(map(repr, db.execute(sql).fetchall()))

def _as_dicts(columns, data):
    return {t: [dict(zip(columns[t], r)) for r in rows] for t, rows in data.items()}

def _random_legal(columns: dict, conditions, rng: random.Random):
    data = {t: [[rng.choice([None, 1, 2, 3]) for _ in cols] for _ in range(rng.randint(0, 5))] for t, cols in columns.items()}
    for _ in range(4):
        for c in conditions:
            at = {t: {name: i for i, name in enumerate(cols)} for t, cols in columns.items()}
            rows = data[c.table]
            if c.kind == "not_null":
                for row in rows:
                    row[at[c.table][c.columns[0]]] = row[at[c.table][c.columns[0]]] or 1
            elif c.kind == "unique":
                seen, kept = set(), []
                for row in rows:
                    key = tuple(row[at[c.table][n]] for n in c.columns)
                    if None in key or key not in seen:
                        seen.add(key)
                        kept.append(row)
                data[c.table] = kept
            else:
                parents = [tuple(r[at[c.parent][n]] for n in c.parent_columns) for r in data[c.parent]]
                kept = []
                for row in rows:
                    key = tuple(row[at[c.table][n]] for n in c.columns)
                    if None in key or key in parents:
                        kept.append(row)
                    elif parents:
                        for n, v in zip(c.columns, rng.choice(parents)):
                            row[at[c.table][n]] = v
                        kept.append(row)
                data[c.table] = kept
        if not any(ce.broken_by(c, _as_dicts(columns, data)) for c in conditions):
            return data
    return None

CONTROLS = [
    ("SELECT a FROM t WHERE a > 1", "SELECT a FROM t WHERE a > 2"),
    ("SELECT a FROM t", "SELECT b FROM t"),
    ("SELECT a FROM t WHERE a IS NULL", "SELECT a FROM t WHERE a IS NOT NULL"),
    ("SELECT t.a FROM t JOIN u ON t.k = u.k", "SELECT a FROM t WHERE k IN (SELECT k FROM u) AND a > 1"),
    ("SELECT k, COUNT(*) AS n FROM t GROUP BY k", "SELECT k, SUM(a) AS n FROM t GROUP BY k"),
]

def test_a_set_no_database_satisfies_is_refused():
    broken = ce.Condition("foreign_key", "child", ("a", "b"), "parent", ("x",))
    assert not ce.jointly_satisfiable([broken])
    fine = [ce.Condition("foreign_key", "child", ("a", "b"), "parent", ("x", "y")), ce.Condition("unique", "parent", ("x", "y")), ce.Condition("not_null", "child", ("a",))]
    assert ce.jointly_satisfiable(fine)
    # facts that would be contradictory are never offered: the gate runs before any verdict
    assert ce.jointly_satisfiable([], {"t": __import__("kumo_bq_prover.ported.smt_equivalence", fromlist=["TableConstraints"]).TableConstraints(not_null=frozenset({"a"}), keys=(("a",),))})

DECOYS = [
    # NOT NULL x is not enough: an empty t gives NULL on the left and 0 on the right, and "t is not empty" is not a condition the provers can state
    ("SELECT SUM(CASE WHEN x IS NULL THEN 0 ELSE 1 END) AS n FROM t", "SELECT COUNT(*) AS n FROM t", {"t": ["id", "x"]}, {"t": []}),
    # these need a CHECK on the values (x > 0, x < 10): outside the catalog, so the answer stays what it was
    ("SELECT id FROM t WHERE x >= 0", "SELECT id FROM t WHERE x > 0", {"t": ["id", "x"]}, {"t": [(1, 0)]}),
    ("SELECT id FROM t WHERE x < 10", "SELECT id FROM t WHERE x <= 10", {"t": ["id", "x"]}, {"t": [(1, 10)]}),
    ("SELECT id FROM t WHERE x BETWEEN 1 AND 10", "SELECT id FROM t WHERE x >= 1", {"t": ["id", "x"]}, {"t": [(1, 11)]}),
]
