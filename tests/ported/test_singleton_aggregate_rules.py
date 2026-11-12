from collections import Counter

import pytest

import sqlglot

from kumo_bq_prover.ported.singleton_aggregate_rules import COLLATION_ASSUMPTION, singleton_count_sum

from kumo_bq_prover.ported.algebraic_equivalence import prove_equivalent_algebraic

from kumo_bq_prover.ported.smt_equivalence import TableConstraints

SCHEMA={"e":["id","job","sal"],"d":["name","extra"]}

KEYS={"e":[("id",)]};TYPES={"e":{"id":"INT64","job":"STRING","sal":"INT64"},"d":{"name":"STRING","extra":"INT64"}}

KEYS={"e":[("id",)]};TYPES={"e":{"id":"INT64","job":"STRING","sal":"INT64"},"d":{"name":"STRING","extra":"INT64"}}

QUERY="SELECT a.job,a.sal*b.n AS s FROM (SELECT job,sal FROM e WHERE id=10) a JOIN (SELECT name,COUNT(*) AS n FROM d GROUP BY name) b ON a.job=b.name"

FLAT="SELECT e.job,SUM(e.sal) AS s FROM e JOIN d ON e.job=d.name WHERE e.id=10 GROUP BY e.job,d.name"

def rewrite(sql=QUERY,keys=KEYS,types=TYPES):
    return singleton_count_sum(sqlglot.parse_one(sql),keys,types,set())

@pytest.mark.parametrize("sql",[QUERY.replace("id=10","id>10"),QUERY.replace("id=10","id=NULL"),
    QUERY.replace("JOIN (","LEFT JOIN ("),QUERY.replace("GROUP BY name","GROUP BY name,extra"),
    QUERY.replace("COUNT(*)","COUNT(extra)"),QUERY.replace("COUNT(*)","COUNT(DISTINCT extra)"),
    QUERY.replace("id=10","id=10 OR id=11"),QUERY.replace("a.job,a.sal","b.name,a.sal"),
    QUERY.replace("GROUP BY name","GROUP BY ROLLUP(name)"),QUERY+" LIMIT 1"])
def test_boundaries_decline(sql):
    assert rewrite(sql) is None

def test_metadata_boundaries_decline():
    assert rewrite(keys={}) is None
    assert rewrite(keys={"e":[("id","job")]}) is None
    assert rewrite(types={"e":{"sal":"FLOAT64"}}) is None
    assert rewrite(types={}) is None
    assert rewrite(types={"e":{"id":"STRING","sal":"INT64"}}) is None
    assert rewrite(types={"e":{"id":"FLOAT64","sal":"INT64"}}) is None
    assert rewrite(QUERY.replace("id=10","id='10'")) is None
    assert rewrite(QUERY.replace("id=10","id=1.0")) is None
    assert rewrite(QUERY.replace("id=10","id=9007199254740993")) is None
    assert rewrite(types={"e":{"id":"INT64","job":"INT64","sal":"INT64"},"d":{"name":"STRING"}}) is None
    assert rewrite(types={"e":{"id":"INT64","job":"INT64","sal":"INT64"},"d":{"name":"FLOAT64"}}) is None
