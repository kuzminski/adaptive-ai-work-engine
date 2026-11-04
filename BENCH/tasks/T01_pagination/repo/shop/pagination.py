"""Pagination helpers.

Pages are 1-based: page 1 holds the first `per_page` items. A page past the end is empty,
the last page may be partial. Invalid `page` or `per_page` (< 1) raise ValueError.
`page_count(total, per_page)` is the number of pages needed for `total` items.
"""


def paginate(items, page, per_page):
    start = page * per_page
    return list(items)[start:start + per_page]
