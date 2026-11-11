import decimal

from kumo_bq_prover.values import decode_rows, same_rows

SCHEMA = {
    "fields": [
        {"name": "id", "type": "INTEGER", "mode": "NULLABLE"},
        {"name": "amount", "type": "NUMERIC", "mode": "NULLABLE"},
        {"name": "tags", "type": "STRING", "mode": "REPEATED"},
        {
            "name": "addr",
            "type": "RECORD",
            "mode": "NULLABLE",
            "fields": [{"name": "city", "type": "STRING"}, {"name": "zip", "type": "STRING"}],
        },
        {"name": "ts", "type": "TIMESTAMP", "mode": "NULLABLE"},
    ]
}


def rows():
    return [
        {
            "f": [
                {"v": "1"},
                {"v": "10.50"},
                {"v": [{"v": "x"}, {"v": "y"}]},
                {"v": {"f": [{"v": "Oslo"}, {"v": None}]}},
                {"v": "1.7040672E9"},
            ]
        },
        {"f": [{"v": "2"}, {"v": None}, {"v": []}, {"v": None}, {"v": None}]},
    ]


def test_decode_scalars_arrays_structs():
    first, second = decode_rows(SCHEMA, rows())
    assert first[0] == 1
    assert first[1] == decimal.Decimal("10.50")
    assert first[2] == ("ARRAY", ("x", "y"))
    assert first[3] == ("STRUCT", (("city", "Oslo"), ("zip", None)))
    assert first[4] == 1704067200000000
    assert second[1:] == (None, ("ARRAY", ()), None, None)


def test_multiset_ignores_order_but_not_multiplicity():
    a = [(1,), (2,), (2,)]
    assert same_rows(a, [(2,), (1,), (2,)])
    assert not same_rows(a, [(1,), (2,)])
    assert not same_rows(a, [(2,), (1,), (2,)], ordered=True)


def test_nan_equals_nan_and_negative_zero_equals_zero():
    assert same_rows([(float("nan"),)], [(float("nan"),)])
    assert same_rows([(-0.0,)], [(0.0,)])


def test_null_is_not_zero_or_empty_array():
    assert not same_rows([(None,)], [(0,)])
    assert not same_rows([(None,)], [(("ARRAY", ()),)])
