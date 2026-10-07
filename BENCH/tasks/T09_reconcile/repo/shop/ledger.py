def reconcile(statement, ledger, max_days=2):
    """Match bank statement lines to ledger entries.

    statement: list of {"id": str, "date": datetime.date, "amount": Decimal|str|int, "memo": str}
    ledger:    list of {"id": str, "date": datetime.date, "amount": Decimal|str|int, "ref": str}

    A line and an entry may match only when their amounts are equal as Decimals and their dates differ
    by at most `max_days`. Matching is one-to-one and greedy: statement lines are processed in order of
    (date, id); for each, among the still unmatched candidate entries the winner is
      1. an entry whose `ref` (non-empty, compared case-insensitively) occurs in the line's memo;
      2. otherwise the entry with the smallest date difference;
      3. remaining ties: the smallest entry id (string order).
    Returns {"matched": [(statement_id, ledger_id), ...] sorted by statement id,
             "unmatched_statement": [ids, sorted], "unmatched_ledger": [ids, sorted]}   (string order).
    ValueError if max_days < 0. The inputs are not mutated.
    """
    raise NotImplementedError
