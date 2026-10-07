from shop.pagination import paginate


def test_first_page_has_the_first_items():
    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]


def test_empty_input():
    assert paginate([], 1, 3) == []
