import sqlglot
from sqlglot import exp

from kumo_bq_prover import pairgen
from kumo_bq_prover.schema_infer import prepare

QUERY = """
SELECT category, COUNT(*) AS n, IF(MAX(price) > 10, 'high', 'low') AS band,
       CASE WHEN SUM(qty) > 5 THEN 'many' ELSE 'few' END AS volume
FROM `p.d.items` AS i JOIN `p.d.orders` AS o ON i.id = o.item_id
WHERE o.day BETWEEN '2024-01-01' AND '2024-01-31' AND i.kind IN ('a', 'b') AND i.price <> 0
GROUP BY category
ORDER BY n DESC
LIMIT 3
"""


def tree():
    return prepare(QUERY).tree


def rules(kind):
    return {v.rule: v for v in pairgen.variants(tree(), max_mutations=40) if v.kind == kind}


def test_every_variant_is_valid_bigquery_and_differs_from_the_original():
    base = pairgen.to_sql(tree())
    for variant in pairgen.variants(tree(), max_mutations=40):
        sqlglot.parse_one(variant.sql, read="bigquery")
        assert variant.sql != base


def test_rewrites_cover_the_identities_the_query_offers():
    names = set(rules("equivalent"))
    assert {"between-expanded", "in-list-as-or", "not-equal-as-not-equal-to", "if-as-case", "case-as-if", "wrap-in-cte", "all-rewrites-together"} <= names


def test_between_and_in_expansions_read_as_expected():
    equivalent = rules("equivalent")
    assert ">=" in equivalent["between-expanded"].sql and "BETWEEN" not in equivalent["between-expanded"].sql
    assert " OR " in equivalent["in-list-as-or"].sql and " IN (" not in equivalent["in-list-as-or"].sql
    assert "NOT (" in equivalent["not-equal-as-not-equal-to"].sql


def test_if_as_case_does_not_treat_case_branches_as_if_calls():
    out = pairgen.apply_everywhere(tree(), next(r for r in pairgen.EQUIVALENT_RULES if r.name == "if-as-case"))
    assert out is not None
    sql = pairgen.to_sql(out)
    assert sql.count("CASE") == 2  # the IF became one CASE, the existing CASE stayed one


def test_wrap_in_cte_and_rename():
    wrapped = rules("equivalent")["wrap-in-cte"].sql
    assert wrapped.startswith("WITH q_wrapped_ AS (SELECT")
    renamed = pairgen._rename_ctes(prepare("WITH base AS (SELECT a FROM `p.d.t`) SELECT base.a FROM base").tree)
    sql = pairgen.to_sql(renamed)
    assert "base_cte" in sql and "AS base" in sql


def test_mutations_change_something_the_oracle_can_see():
    mutations = rules("mutation")
    assert {"flip-boundary", "and-as-or", "drop-right-conjunct", "inner-join-as-left-join", "flip-order-direction", "bump-limit"} <= set(mutations)
    assert "LEFT JOIN" in mutations["inner-join-as-left-join"].sql
    assert "LIMIT 4" in mutations["bump-limit"].sql
    assert "DESC" not in mutations["flip-order-direction"].sql.split("ORDER BY")[1]


def test_mutation_count_is_bounded_and_spread_over_rules():
    picked = [v for v in pairgen.variants(tree(), max_mutations=5) if v.kind == "mutation"]
    assert len(picked) <= 5
    assert len({v.rule for v in picked}) == len(picked)


def test_apply_at_changes_exactly_one_site():
    rule = next(r for r in pairgen.MUTATIONS if r.name == "and-as-or")
    before = len(pairgen.sites(tree(), rule))
    one = pairgen.apply_at(tree(), rule, 0)
    assert before >= 2 and len(list(one.find_all(exp.Or))) == 1
