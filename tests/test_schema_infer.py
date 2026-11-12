import pytest

from kumo_bq_prover.schema_infer import Unsupported, prepare, type_sql

GA4_SHAPE = """
SELECT ep.value.string_value AS page, COUNT(*) AS n
FROM `some-project.some_dataset.events_*` AS e, UNNEST(e.event_params) AS ep
WHERE ep.key = 'page_title'
  AND _TABLE_SUFFIX BETWEEN '20210101' AND '20210107'
  AND e.event_name = 'page_view'
GROUP BY page
"""


def schema_of(sql):
    return prepare(sql).schema.local_schema()


def test_nested_array_of_struct_is_inferred_from_unnest_and_paths():
    schema = schema_of(GA4_SHAPE)
    (table,) = schema.values()
    params = table["event_params"]
    assert params[0] == "ARRAY" and params[1][0] == "STRUCT"
    fields = dict(params[1][1])
    assert fields["key"] == "STRING"
    assert dict(fields["value"][1]) == {"string_value": "STRING"}
    assert type_sql(params).startswith("ARRAY<STRUCT<")
    assert table["event_name"] == "STRING"


def test_wildcard_table_suffix_becomes_a_column_and_tables_are_renamed():
    prepared = prepare(GA4_SHAPE)
    assert "t_0" in prepared.sql and "some-project" not in prepared.sql and "_TABLE_SUFFIX" not in prepared.sql
    assert prepared.schema.local_schema()["t_0"]["tsuffix_"] == "STRING"


def test_unnest_alias_with_the_name_of_its_column_still_reads_the_column():
    schema = schema_of("SELECT h.type FROM `p.d.sessions` CROSS JOIN UNNEST(hits) AS h WHERE h.type = 'PAGE'")
    (table,) = schema.values()
    assert table["hits"] == ("ARRAY", ("STRUCT", (("type", "STRING"),)))
    schema = schema_of("SELECT hits.type FROM `p.d.sessions` CROSS JOIN UNNEST(hits) AS hits")
    assert "hits" in next(iter(schema.values()))


def test_types_flow_through_a_cte_to_the_table_column():
    schema = schema_of("WITH s AS (SELECT region, amount FROM `p.d.sales`) SELECT region, SUM(amount) FROM s GROUP BY region")
    (table,) = schema.values()
    assert table["amount"] == "INT64" and table["region"] == "STRING"


def test_string_constants_do_not_override_date_evidence():
    schema = schema_of("SELECT EXTRACT(YEAR FROM d) FROM `p.d.t` WHERE d BETWEEN '2020-01-01' AND '2020-12-31'")
    assert next(iter(schema.values()))["d"] == "DATE"


def test_numeric_and_boolean_evidence():
    schema = schema_of("SELECT AVG(x) FROM `p.d.t` WHERE flag AND y > 2.5 AND n >= 3")
    (table,) = schema.values()
    assert table["flag"] == "BOOL" and table["y"] == "FLOAT64" and table["n"] == "INT64" and table["x"] == "INT64"


def test_join_columns_are_linked_and_classes_are_shared():
    prepared = prepare("SELECT a.v FROM `p.d.a` AS a JOIN `p.d.b` AS b ON a.k = b.k2 WHERE b.k2 = 5")
    schema = prepared.schema.local_schema()
    assert schema["t_0"]["k"] == "INT64" and schema["t_1"]["k2"] == "INT64"
    classes = prepared.schema.classes()
    assert classes[("t_0", "k")] == classes[("t_1", "k2")]


def test_declared_variables_are_inlined():
    prepared = prepare("DECLARE d STRING DEFAULT '20200101';\nSELECT x FROM `p.d.t_*` WHERE _TABLE_SUFFIX = d")
    assert "20200101" in prepared.sql and "DECLARE" not in prepared.sql.upper()


def test_string_columns_that_are_parsed_remember_their_format():
    prepared = prepare("SELECT PARSE_DATE('%Y%m%d', d), CAST(n AS INT64) FROM `p.d.t`")
    flavours = prepared.schema.flavours()
    assert flavours[("t_0", "d")] == "%Y%m%d" and flavours[("t_0", "n")] == "int"


def test_date_like_columns_carry_alternatives_for_a_retry():
    prepared = prepare("SELECT DATE_DIFF(d, e, DAY) FROM `p.d.t`")
    options = list(prepared.schema.variants())
    assert options[0] == {} and len(options) > 1
    kinds = {prepared.schema.local_schema(choice)["t_0"]["d"] for choice in options}
    assert kinds == {"DATE", "TIMESTAMP", "DATETIME"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ST_AREA(g) FROM `p.d.t`",
        "CREATE TEMP FUNCTION f(x INT64) AS (x + 1);\nSELECT f(a) FROM `p.d.t`",
        "SELECT 1; SELECT 2",
    ],
)
def test_queries_the_emulator_cannot_run_are_reported_not_guessed(sql):
    with pytest.raises(Unsupported):
        prepare(sql)
