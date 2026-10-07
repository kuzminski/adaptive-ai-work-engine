import csv
import io
from decimal import Decimal

from shop.report import to_csv, to_rows


def test_header_only_for_empty_input():
    assert to_csv([]) == "id,customer,total\r\n"


def test_plain_rows_and_two_decimals():
    orders = [{"id": 1, "customer": "Ann", "total": 5},
              {"id": 2, "customer": "Bob", "total": 3.5},
              {"id": 3, "customer": "Cy", "total": Decimal("12.3")}]
    assert to_csv(orders) == "id,customer,total\r\n1,Ann,5.00\r\n2,Bob,3.50\r\n3,Cy,12.30\r\n"


def test_quoting_round_trips():
    orders = [{"id": 7, "customer": 'Smith, "Jo"', "total": 1},
              {"id": 8, "customer": "line\nbreak", "total": 2},
              {"id": 9, "customer": "Zoë", "total": 0}]
    out = to_csv(orders)
    assert out.endswith("\r\n")
    assert '"Smith, ""Jo"""' in out
    rows = list(csv.reader(io.StringIO(out, newline="")))
    assert rows[0] == ["id", "customer", "total"]
    assert rows[1] == ["7", 'Smith, "Jo"', "1.00"]
    assert rows[2] == ["8", "line\nbreak", "2.00"]
    assert rows[3] == ["9", "Zoë", "0.00"]


def test_to_rows_is_unchanged():
    assert to_rows([{"id": 1, "customer": "Ann", "total": 5}]) == [(1, "Ann", 5)]
