"""Pagination helpers (reference solution)."""


def _check(per_page):
    if per_page < 1:
        raise ValueError("per_page must be >= 1")


def paginate(items, page, per_page):
    _check(per_page)
    if page < 1:
        raise ValueError("page must be >= 1")
    start = (page - 1) * per_page
    return list(items)[start:start + per_page]


def page_count(total, per_page):
    _check(per_page)
    return -(-total // per_page)
