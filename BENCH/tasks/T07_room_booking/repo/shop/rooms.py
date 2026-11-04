def assign(bookings, rooms):
    """Assign bookings to `rooms` rooms (indexed 0..rooms-1).

    A booking is a dict {"id": str, "start": int, "end": int, "priority": int} (start < end).

    Bookings are processed in order of (start, -priority, id). A room is free at time s when the booking
    running in it ends at or before s. A booking takes the lowest-index free room. When no room is free,
    the running booking with a strictly lower priority than the new one is evicted: the lowest priority
    first, then the latest end, then the highest room index. The new booking takes the evicted booking's
    room and the evicted id is appended to `rejected`. When there is no such booking, the new booking is
    rejected (appended to `rejected`).

    Returns {"assignments": {id: room_index}, "rejected": [ids in the order they were rejected]};
    evicted bookings are not in `assignments`. ValueError for rooms < 1, start >= end or duplicate ids.
    """
    raise NotImplementedError
