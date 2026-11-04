def assign(bookings, rooms):
    """Reference solution."""
    if rooms < 1:
        raise ValueError("rooms must be >= 1")
    seen = set()
    for item in bookings:
        if item["start"] >= item["end"]:
            raise ValueError("start must be before end")
        if item["id"] in seen:
            raise ValueError("duplicate booking id")
        seen.add(item["id"])
    running = [None] * rooms
    assignments, rejected = {}, []
    for item in sorted(bookings, key=lambda x: (x["start"], -x["priority"], x["id"])):
        for index, current in enumerate(running):
            if current is not None and current["end"] <= item["start"]:
                running[index] = None
        free = [index for index, current in enumerate(running) if current is None]
        if free:
            running[free[0]] = item
            assignments[item["id"]] = free[0]
            continue
        candidates = [(cur["priority"], -cur["end"], -index, index) for index, cur in enumerate(running)
                      if cur["priority"] < item["priority"]]
        if not candidates:
            rejected.append(item["id"])
            continue
        index = min(candidates)[3]
        victim = running[index]
        rejected.append(victim["id"])
        del assignments[victim["id"]]
        running[index] = item
        assignments[item["id"]] = index
    return {"assignments": assignments, "rejected": rejected}
