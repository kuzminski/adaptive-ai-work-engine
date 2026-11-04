from decimal import Decimal


def _amount(value):
    return Decimal(str(value))


def reconcile(statement, ledger, max_days=2):
    """Reference solution."""
    if max_days < 0:
        raise ValueError("max_days must be >= 0")
    free = {entry["id"]: entry for entry in ledger}
    matched, unmatched_statement = [], []
    for line in sorted(statement, key=lambda item: (item["date"], item["id"])):
        candidates = [e for e in free.values() if _amount(e["amount"]) == _amount(line["amount"])
                      and abs((e["date"] - line["date"]).days) <= max_days]
        if not candidates:
            unmatched_statement.append(line["id"])
            continue
        memo = str(line.get("memo", "")).lower()

        def rank(entry):
            ref = str(entry.get("ref", "")).lower()
            return (0 if ref and ref in memo else 1, abs((entry["date"] - line["date"]).days), str(entry["id"]))

        best = min(candidates, key=rank)
        del free[best["id"]]
        matched.append((line["id"], best["id"]))
    return {"matched": sorted(matched, key=lambda pair: str(pair[0])),
            "unmatched_statement": sorted(unmatched_statement, key=str),
            "unmatched_ledger": sorted(free, key=str)}
