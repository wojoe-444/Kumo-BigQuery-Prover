import sqlglot

from kumo_bq_prover.datagen import random_database
from kumo_bq_prover.harvest import harvest

SCHEMA = {
    "orders": {"id": "INTEGER", "total": "INTEGER", "ordered_at": "TIMESTAMP"},
    "products": {"id": "INTEGER", "name": "VARCHAR", "code": "VARCHAR", "price": "NUMERIC", "sold_on": "DATE"},
}


def test_date_literal_against_a_timestamp_column_gives_both_ends_of_the_day():
    found = harvest("SELECT 1 FROM orders WHERE CAST(ordered_at AS DATE) = DATE '2020-01-01'", "SELECT 1", SCHEMA)
    assert "2020-01-01 00:00:00+00" in found["ordered_at"]
    assert "2020-01-01 23:59:59+00" in found["ordered_at"]
    assert "2020-01-02 00:00:00+00" in found["ordered_at"]


def test_between_in_like_and_prefix_tests_are_read():
    sql = "SELECT 1 FROM products WHERE SUBSTR(code, 1, 3) = 'HN-' AND name LIKE 'Hex%' AND id IN (3, 5) AND price > 1.5"
    found = harvest(sql, "SELECT 1", SCHEMA)
    assert "HN-1" in found["code"] and "Hex" in found["name"]
    assert {2, 3, 4, 5, 6} <= set(found["id"])
    assert "2.0" in found["price"] or "1.5" in found["price"]


def test_numeric_constants_with_more_than_nine_decimals_are_not_offered():
    found = harvest("SELECT 1 FROM products WHERE price = 0.0000000005", "SELECT 1", SCHEMA)
    assert "price" not in found


def test_comparisons_between_two_columns_and_bad_sql_add_nothing():
    assert harvest("SELECT 1 FROM orders WHERE id = total", "this is not sql (", SCHEMA) == {}


def test_harvested_data_still_parses_as_bigquery():
    found = harvest(
        "SELECT 1 FROM products WHERE sold_on > DATE '2024-02-29' AND name = 'it''s'", "SELECT 1", SCHEMA
    )
    for seed in range(10):
        for statement in random_database(SCHEMA, seed, extra=found):
            sqlglot.parse_one(statement, read="bigquery")
