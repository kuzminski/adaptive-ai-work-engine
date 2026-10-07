from shop.inventory import Inventory


def test_add_and_available():
    inv = Inventory()
    inv.add("x", 5)
    assert inv.available("x") == 5
