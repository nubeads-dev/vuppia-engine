def rank_memories(item):
    return max(0.0, min(1.0, 1.0 - float(item.get("overlap", 0.0))))
