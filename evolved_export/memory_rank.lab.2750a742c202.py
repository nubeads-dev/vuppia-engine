def rank_memories(item):
    overlap = float(item.get("overlap", 0.0))
    recency = float(item.get("recency", 0.0))
    trust = float(item.get("trust", 0.0))
    specificity = float(item.get("specificity", 0.0))
    contradiction = float(item.get("contradiction", 0.0))
    base = 0.45*overlap + 0.10*recency + 0.30*trust + 0.15*specificity
    return max(0.0, min(1.0, base * (1.0 - 0.9*contradiction)))
