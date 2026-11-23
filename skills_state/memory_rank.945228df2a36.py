"""Skill inicial de recuperación de memoria: ordena recuerdos por relevancia a una consulta.

Ingenua a propósito: prioriza casi solo el solapamiento. La evolución puede mejorarla
(p. ej. ponderar la confianza y penalizar contradicciones). No mide conciencia.
La evolución reemplaza SOLO esta función, en el canal LAB.
"""

def rank_memories(item):
    overlap = float(item.get("overlap", 0.0))
    recency = float(item.get("recency", 0.0))
    return max(0.0, min(1.0, 0.9 * overlap + 0.1 * recency))

# cambio cosmetico
