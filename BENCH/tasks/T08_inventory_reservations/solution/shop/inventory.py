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
        return stock.available if stock else 0

    def reserve(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > self.available(sku):
            raise OutOfStock(sku)
        self._stock[sku].reserved += qty
        rid = next(self._ids)
        self._reservations[rid] = (sku, qty)
        return rid

    def release(self, rid):
        sku, qty = self._reservations.pop(rid)
        self._stock[sku].reserved -= qty

    def commit(self, rid):
        sku, qty = self._reservations.pop(rid)
        stock = self._stock[sku]
        stock.reserved -= qty
        stock.on_hand -= qty
        return qty
