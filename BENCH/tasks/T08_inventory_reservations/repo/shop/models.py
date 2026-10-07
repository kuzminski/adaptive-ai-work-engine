from dataclasses import dataclass


class OutOfStock(Exception):
    pass


@dataclass
class Stock:
    sku: str
    on_hand: int = 0
    reserved: int = 0

    @property
    def available(self):
        return self.on_hand - self.reserved
