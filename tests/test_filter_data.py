"""filter_data: rows that satisfy a query's own filters, joins and UNNESTs.

The planner tests need no emulator. The live tests (``pytest -m emulator``) check that the databases
make queries return rows that plain random data leaves empty.
"""

import math
import random

import pytest
import sqlglot

from kumo_bq_prover import filter_data as fd
from kumo_bq_prover import nested_datagen, spider2
from kumo_bq_prover.emulator import running_emulator
from kumo_bq_prover.harvest import harvest
from kumo_bq_prover.schema_infer import prepare


def planner(sql: str):
    prepared = prepare(sql)
    found = fd.Planner(prepared.tree, prepared.schema.local_schema())
    found.walk()
    return prepared, found


COHORT = """
WITH a AS (SELECT id, grp FROM `p.d.events` WHERE kind = 'open' AND EXTRACT(YEAR FROM day) = 2022),
     b AS (SELECT id, grp FROM `p.d.events` WHERE kind = 'close' AND EXTRACT(YEAR FROM day) = 2023)
SELECT a.grp, COUNT(*) AS n FROM a JOIN b ON a.id = b.id GROUP BY a.grp
"""


def test_two_readings_of_one_table_are_two_anchors_with_their_own_filters_and_one_join_key():
    _, found = planner(COHORT)
    assert len([a for a in found.anchors.values() if not a.orphan]) == 2
    texts = sorted(atom.sql(dialect="bigquery") for atom, _, _ in found.atoms)
    assert len(texts) == 4 and sum("'open'" in t for t in texts) == 1 and sum("'close'" in t for t in texts) == 1
    ids = [key for key in found.vars if key[1] == ("id",)]
    assert len(ids) == 2 and found.root(ids[0]) == found.root(ids[1])  # a.id = b.id links them


def test_a_cte_read_twice_is_two_instances_that_can_hold_different_rows():
    _, found = planner(
        "WITH s AS (SELECT id, name FROM `p.d.stops`) SELECT 1 FROM s AS a JOIN s AS b ON a.id = b.id WHERE a.name = 'x' AND b.name = 'y'"
    )
    rows = [a for a in found.anchors.values() if a.parent is None and not a.orphan]
    assert len(rows) == 2
    equalities = {atom.sql(dialect="bigquery").split(" = ")[0]: atom.sql(dialect="bigquery") for atom, _, _ in found.atoms}
    assert len(equalities) == 2  # a.name = 'x' and b.name = 'y' constrain different variables


def test_a_filter_on_an_output_expression_is_substituted_into_the_atom():
    _, found = planner(
        "WITH s AS (SELECT EXTRACT(YEAR FROM day) AS yr, amount FROM `p.d.sales`) SELECT SUM(amount) FROM s WHERE yr = 2021"
    )
    (atom, keys, outer), *_ = found.atoms
    assert "EXTRACT(YEAR FROM" in atom.sql(dialect="bigquery") and "= 2021" in atom.sql(dialect="bigquery")
    assert outer  # the filter is read through a derived table


def test_a_count_distinct_threshold_marks_the_counted_variable_for_different_values():
    _, found = planner("SELECT g FROM `p.d.t` GROUP BY g HAVING COUNT(DISTINCT u) > 4")
    assert [key[1] for key in found.diversify] == [("u",)]


def test_a_rank_filter_asks_for_more_groups():
    _, found = planner("SELECT g FROM (SELECT g, ROW_NUMBER() OVER (ORDER BY v DESC) AS r FROM `p.d.t`) WHERE r = 3")
    assert found.min_scenarios >= 5


def test_unnest_becomes_an_element_anchor_inside_its_parent_row():
    prepared, found = planner(
        "SELECT p.value.n FROM `p.d.events` AS e, UNNEST(e.params) AS p WHERE p.key = 'k' AND p.value.n > 3"
    )
    elems = [a for a in found.anchors.values() if a.parent is not None]
    assert len(elems) == 1 and elems[0].parent_path == ("params",)
    assert {key[1] for key in found.vars} == {("key",), ("value", "n")}
    assert all(not a.orphan for a in found.anchors.values())


def test_shadowed_unnest_alias_still_reaches_the_column():
    _, found = planner("SELECT COUNT(*) FROM `p.d.t` AS t CROSS JOIN UNNEST(t.hits) AS hits WHERE hits.type = 'PAGE'")
    assert [a for a in found.anchors.values() if a.parent is not None and not a.orphan]
    _, found = planner("SELECT COUNT(*) FROM `p.d.t`, UNNEST(hits) AS hits WHERE hits.type = 'PAGE'")
    assert [a for a in found.anchors.values() if a.parent is not None and not a.orphan]


def test_having_count_and_a_derived_count_ask_for_bigger_groups():
    _, found = planner("SELECT g FROM `p.d.t` GROUP BY g HAVING COUNT(*) > 10")
    assert [t.count for t in found.thresholds] == [11]
    _, found = planner("SELECT g, c FROM (SELECT g, COUNT(*) AS c FROM `p.d.t` GROUP BY g) WHERE c >= 50")
    assert [t.count for t in found.thresholds] == [50]
    _, found = planner("SELECT g FROM `p.d.t` GROUP BY g HAVING COUNT(DISTINCT u) > 4")
    assert [(t.count, t.distinct) for t in found.thresholds] == [(5, True)]


def test_an_atom_with_a_window_aggregate_or_unreadable_subquery_is_skipped_not_fatal():
    _, found = planner(
        "SELECT x FROM `p.d.t` WHERE x IN (SELECT y FROM `p.d.u`) AND RANK() OVER (ORDER BY x) = 1 AND z = 5"
    )
    assert found.skipped >= 1 and len(found.atoms) == 1


def test_a_parsed_or_cast_string_gets_its_format_even_through_min():
    _, found = planner(
        "WITH s AS (SELECT MIN(d) AS first FROM `p.d.t`) SELECT PARSE_DATE('%Y%m%d', first), CAST(n AS INT64) FROM s, `p.d.u`"
    )
    flavours = found.flavours()
    assert "%Y%m%d" in flavours.values()


def test_constants_of_a_filter_reach_the_candidates():
    rng = random.Random(0)
    atom = sqlglot.parse_one("EXTRACT(YEAR FROM s.v) = 2019 AND s.v <> DATE '2019-03-01'", read="bigquery")
    assert any(str(d).startswith("2019") for d in fd.candidates("DATE", [atom], rng))
    like = sqlglot.parse_one("REGEXP_CONTAINS(s.v, r'^/home') OR s.v LIKE '%shop%'", read="bigquery")
    pool = fd.candidates("STRING", [like], rng)
    assert "/home" in pool and "shop" in pool
    assert "06" in fd.candidates("STRING", [sqlglot.parse_one("CAST(s.v AS INT64) = 6", read="bigquery")], rng)


def test_regex_samples_strip_anchors_and_classes():
    assert fd.regex_samples("^/home") == ["/home"]
    assert fd.regex_samples("alibaba|amazon") == ["alibaba", "amazon"]
    assert fd.regex_samples("plain text") == []


class _AllPass:
    """A stand-in for the emulator that says every candidate passes (offline tests of the assembly)."""

    class _Result:
        def __init__(self, rows):
            self.rows = rows

    def query(self, sql, dataset=None):
        return self._Result([(i,) for i in range(sql.count(" AS i"))])


def test_database_assembly_builds_valid_nested_inserts_with_repeated_groups():
    prepared = prepare(
        "SELECT e.user, COUNT(*) FROM `p.d.events` AS e, UNNEST(e.params) AS p WHERE p.key = 'k' GROUP BY e.user HAVING COUNT(*) > 4"
    )
    schema = prepared.schema.local_schema()
    plan = fd.plan(_AllPass(), prepared.tree, schema)
    setup = plan.database(1, classes=prepared.schema.classes())
    assert setup[0].startswith("CREATE TABLE") and any(s.startswith("INSERT INTO t_0") for s in setup)
    insert = next(s for s in setup if s.startswith("INSERT"))
    assert "STRUCT(" in insert and " AS key" in insert
    assert plan.repeat and math.prod(plan.repeat.values()) >= 5  # HAVING COUNT(*) > 4: the repeated rows and elements make groups of five
    assert plan.database(1, classes=prepared.schema.classes()) == setup  # same seed, same database


def test_a_query_nothing_can_be_read_from_still_gets_a_plan():
    prepared = prepare("SELECT 1 AS one")
    plan = fd.plan(_AllPass(), prepared.tree, prepared.schema.local_schema())
    assert not plan.solvable() and plan.database(0) == []


# --- live: needs the emulator ------------------------------------------------------------------------

EMPTY_ON_RANDOM_DATA = """
WITH opened AS (
  SELECT user_id, EXTRACT(YEAR FROM day) AS yr FROM `p.d.events` WHERE kind = 'open' AND REGEXP_CONTAINS(page, r'^/home') AND EXTRACT(YEAR FROM day) = 2022
),
closed AS (
  SELECT user_id, EXTRACT(YEAR FROM day) AS yr FROM `p.d.events` WHERE kind = 'close' AND EXTRACT(YEAR FROM day) = 2023
)
SELECT o.user_id, COUNT(*) AS n
FROM opened AS o JOIN closed AS c ON o.user_id = c.user_id
GROUP BY o.user_id
HAVING COUNT(*) > 3
"""


@pytest.mark.emulator
def test_the_builder_makes_a_query_return_rows_that_random_data_leaves_empty():
    prepared = prepare(EMPTY_ON_RANDOM_DATA)
    with running_emulator() as client:
        schema, reason = spider2.choose_schema(client, prepared)
        assert schema is not None, reason
        flat = nested_datagen.flatten(schema)
        extra = harvest(prepared.sql, prepared.sql, flat)
        exact = harvest(prepared.sql, prepared.sql, flat, exact=True)
        classes, flavours = prepared.schema.classes(), prepared.schema.flavours()

        def returns_rows(setup):
            with client.scratch_dataset() as dataset:
                for statement in setup:
                    client.query(statement, dataset)
                return bool(client.query(prepared.sql, dataset).rows)

        random_hits = sum(
            returns_rows(nested_datagen.random_database(schema, seed, extra=extra, classes=classes, exact=exact, flavours=flavours))
            for seed in range(6)
        )
        plan = fd.plan(client, prepared.tree, schema, flavours=flavours)
        assert plan.solvable()
        built_hits = sum(returns_rows(plan.database(seed, extra=extra, classes=classes, exact=exact, flavours=flavours)) for seed in range(6))
    assert random_hits == 0
    assert built_hits >= 4


@pytest.mark.emulator
def test_run_query_counts_the_rows_of_the_built_databases_and_refutes_no_rewrite():
    with running_emulator() as client:
        run = spider2.run_query(client, "synthetic", sql=EMPTY_ON_RANDOM_DATA, databases=8)
    assert run.status == "ok", run.reason
    assert run.databases_nonempty_filtered == run.databases_nonempty > 0
    assert run.builder["components_solved"] > 0
    assert not [p for p in run.pairs if p["oracle"] in ("WRONG", "FALSE_PROOF")]
