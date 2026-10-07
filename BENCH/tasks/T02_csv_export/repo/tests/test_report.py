from shop.report import to_rows


def test_to_rows():
    assert to_rows([{"id": 1, "customer": "Ann", "total": 5}]) == [(1, "Ann", 5)]
