import sqlglot

from kumo_bq_prover.datagen import fixed_database, literal, normalize_type, random_database

SCHEMA = {"t": {"x": "INT64", "f": "FLOAT64", "n": "NUMERIC", "s": "VARCHAR", "c": "BOOLEAN", "d": "DATE", "ts": "TIMESTAMP"}}


def test_same_seed_same_database_and_other_seed_differs():
    assert random_database(SCHEMA, 3) == random_database(SCHEMA, 3)
    assert any(random_database(SCHEMA, 3) != random_database(SCHEMA, s) for s in range(4, 12))


def test_every_statement_parses_as_bigquery():
    for seed in range(10):
        for statement in random_database(SCHEMA, seed):
            sqlglot.parse_one(statement, read="bigquery")


def test_types_are_normalised_and_unknown_types_refused():
    assert normalize_type("INTEGER") == "INT64" and normalize_type("varchar(20)") == "STRING"
    try:
        normalize_type("GEOGRAPHY")
    except ValueError:
        return
    raise AssertionError("an unsupported type must be refused, not guessed")


def test_literals_are_typed_and_escaped():
    assert literal("STRING", "it's") == "'it\\'s'"
    assert literal("NUMERIC", "0.1") == "NUMERIC '0.1'"
    assert literal("INT64", None) == "NULL"
    assert literal("TIMESTAMP", "2024-01-01 00:00:00+00") == "TIMESTAMP '2024-01-01 00:00:00+00'"


def test_fixed_database_holds_exactly_the_rows_given():
    statements = fixed_database(SCHEMA, "t", ["x", "s"], [[9007199254740993, "a"]])
    assert statements[-1] == "INSERT INTO t (x, s) VALUES (9007199254740993, 'a')"


def test_extra_values_are_used_and_keep_the_database_deterministic():
    schema = {"t": {"x": "INT64"}}
    extra = {"x": [424242]}
    statements = [s for seed in range(20) for s in random_database(schema, seed, extra=extra)]
    assert any("424242" in s for s in statements)
    assert random_database(schema, 5, extra=extra) == random_database(schema, 5, extra=extra)
    assert all("424242" not in s for seed in range(20) for s in random_database(schema, seed))


def test_specials_are_off_by_default_and_infinities_render_as_ieee_divide():
    schema = {"t": {"f": "FLOAT64", "n": "NUMERIC"}}
    plain = [s for seed in range(60) for s in random_database(schema, seed)]
    assert not any("IEEE_DIVIDE" in s or "NUMERIC '0.000000001'" in s for s in plain)
    special = [s for seed in range(60) for s in random_database(schema, seed, specials=True)]
    assert any("IEEE_DIVIDE(1, 0)" in s for s in special) and any("NUMERIC '0.000000001'" in s for s in special)
    for statement in special:
        sqlglot.parse_one(statement, read="bigquery")


def test_default_databases_hold_no_extreme_floats_but_extreme_ones_can():
    from kumo_bq_prover.datagen import random_database

    schema = {"t": {"f": "FLOAT64"}}
    plain = " ".join(" ".join(random_database(schema, seed, max_rows=20)) for seed in range(30))
    assert "e+308" not in plain and "1e308" not in plain
    extreme = " ".join(" ".join(random_database(schema, seed, max_rows=20, extremes=True)) for seed in range(30))
    assert "1e+308" in extreme
