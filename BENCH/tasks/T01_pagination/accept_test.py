import pytest

from shop.pagination import page_count, paginate


def test_pages_are_one_based_and_last_page_is_partial():
    items = list(range(10))
    assert paginate(items, 1, 3) == [0, 1, 2]
    assert paginate(items, 2, 3) == [3, 4, 5]
    assert paginate(items, 4, 3) == [9]
    assert paginate(items, 5, 3) == []
    assert paginate(items, 99, 3) == []


def test_invalid_arguments_raise():
    for page, per_page in ((0, 3), (-1, 3), (1, 0), (1, -2)):
        with pytest.raises(ValueError):
            paginate([1, 2, 3], page, per_page)


def test_accepts_iterables_and_does_not_mutate():
    assert paginate(iter(range(5)), 2, 2) == [2, 3]
    data = [3, 1, 2]
    paginate(data, 1, 2)
    assert data == [3, 1, 2]


def test_page_count():
    assert page_count(10, 3) == 4
    assert page_count(9, 3) == 3
    assert page_count(0, 3) == 0
    assert page_count(1, 5) == 1
    with pytest.raises(ValueError):
        page_count(5, 0)
