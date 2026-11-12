from kumo_bq_prover import nested_datagen as nd

SCHEMA = {
    "t_0": {
        "id": "INT64",
        "name": "STRING",
        "tags": ("ARRAY", "STRING"),
        "params": ("ARRAY", ("STRUCT", (("key", "STRING"), ("value", ("STRUCT", (("n", "INT64"),)))))),
        "day": "STRING",
    }
}


def test_create_statement_renders_nested_types():
    (statement,) = nd.create_statements(SCHEMA)
    assert "params ARRAY<STRUCT<key STRING, value STRUCT<n INT64>>>" in statement
    assert "tags ARRAY<STRING>" in statement


def test_same_seed_same_database_and_different_seeds_differ():
    a = nd.random_database(SCHEMA, 1)
    assert a == nd.random_database(SCHEMA, 1)
    assert any(a != nd.random_database(SCHEMA, seed) for seed in range(2, 6))


def test_insert_uses_typed_array_and_struct_literals():
    joined = " ".join(nd.random_database(SCHEMA, 3, max_rows=6))
    assert "ARRAY<STRING>[" in joined and "STRUCT(" in joined and " AS key" in joined


def test_harvested_and_exact_values_reach_nested_leaves():
    extra = {"key": ["page_title"]}
    exact = {"key": ["page_title"]}
    seen = " ".join(" ".join(nd.random_database(SCHEMA, s, extra=extra, exact=exact)) for s in range(6))
    assert "'page_title'" in seen


def test_flavoured_strings_parse_with_their_format():
    for flavour in ("%Y%m%d", "%Y", "%Y-%m-%d", "int", "float"):
        for text in nd.flavour_pool(flavour):
            assert nd._parses(flavour, text)
    statements = nd.random_database(SCHEMA, 2, flavours={("t_0", "day"): "%Y%m%d"}, exact={"day": ["20210107", "nonsense"]})
    joined = " ".join(statements)
    assert "nonsense" not in joined


def test_linked_columns_share_a_pool():
    schema = {"a": {"k": "INT64"}, "b": {"k2": "INT64"}}
    classes = {("a", "k"): 7, ("b", "k2"): 7}
    gen = nd._Generator(0, {}, classes, {}, {})
    assert gen.pool(("a", "k"), "k", "INT64") is gen.pool(("b", "k2"), "k2", "INT64")
    assert nd.flatten({"a": {"x": ("ARRAY", ("STRUCT", (("y", "DATE"),)))}}) == {"a": {"y": "DATE"}}
    assert schema
