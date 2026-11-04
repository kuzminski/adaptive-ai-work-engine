import itertools

from .models import OutOfStock, Stock


class Inventory:
    def __init__(self):
        self._stock = {}
        self._reservations = {}
        self._ids = itertools.count(1)

    def add(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock.setdefault(sku, Stock(sku)).on_hand += qty

    def available(self, sku):
        stock = self._stock.get(sku)
        return stock.on_hand if stock else 0

    def reserve(self, sku, qty):
        stock = self._stock.get(sku)
        if stock is None or stock.on_hand < qty:
            raise OutOfStock(sku)
        stock.reserved += qty
        rid = next(self._ids)
        self._reservations[rid] = (sku, qty)
        return rid

    def release(self, rid):
        sku, qty = self._reservations[rid]
        self._stock[sku].reserved -= qty
