from datetime import date
from decimal import Decimal

import pytest

from shop.ledger import reconcile


def line(id, day, amount, memo=""):
    return {"id": id, "date": date(2026, 3, day), "amount": amount, "memo": memo}


def entry(id, day, amount, ref=""):
    return {"id": id, "date": date(2026, 3, day), "amount": amount, "ref": ref}


def test_negative_window():
    with pytest.raises(ValueError):
        reconcile([], [], max_days=-1)


def test_amounts_compare_by_value_and_window_is_inclusive():
    out = reconcile([line("s1", 10, "10.50"), line("s2", 10, 7)],
                    [entry("l1", 12, Decimal("10.5")), entry("l2", 13, 7)])
    assert out == {"matched": [("s1", "l1")], "unmatched_statement": ["s2"], "unmatched_ledger": ["l2"]}


def test_ref_in_memo_beats_date_distance():
    out = reconcile([line("s1", 10, 5, "Payment INV-77 thanks")],
                    [entry("a", 10, 5, "INV-12"), entry("b", 12, 5, "inv-77")])
    assert out["matched"] == [("s1", "b")] and out["unmatched_ledger"] == ["a"]


def test_empty_ref_never_matches_memo_then_nearest_date_then_smallest_id():
    out = reconcile([line("s1", 10, 5, "anything")], [entry("b", 11, 5, ""), entry("a", 9, 5, "")])
    assert out["matched"] == [("s1", "a")]
    out = reconcile([line("s1", 10, 5)], [entry("z", 11, 5), entry("c", 9, 5), entry("m", 10, 5)])
    assert out["matched"] == [("s1", "m")]


def test_greedy_one_to_one_in_date_then_id_order():
    statement = [line("s2", 10, 5), line("s1", 10, 5), line("s0", 11, 5)]
    ledger = [entry("l1", 10, 5), entry("l2", 11, 5)]
    out = reconcile(statement, ledger)
    # (date, id) order: s1, s2, s0 -> s1 takes l1 (distance 0), s2 takes l2 (distance 1), s0 has nothing left
    assert out == {"matched": [("s1", "l1"), ("s2", "l2")], "unmatched_statement": ["s0"], "unmatched_ledger": []}


def test_outputs_are_sorted_as_strings_and_inputs_untouched():
    statement = [line("s10", 5, 1), line("s9", 5, 2)]
    ledger = [entry("l9", 5, 2), entry("l10", 5, 1), entry("l11", 5, 3)]
    snapshot = ([dict(s) for s in statement], [dict(e) for e in ledger])
    out = reconcile(statement, ledger, max_days=0)
    assert out["matched"] == [("s10", "l10"), ("s9", "l9")]
    assert out["unmatched_ledger"] == ["l11"] and out["unmatched_statement"] == []
    assert (statement, ledger) == snapshot
