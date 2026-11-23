"""Skill inicial: priorizar hipótesis por evidencia y falsabilidad. No mide conciencia.
La evolución puede reemplazar SOLO esta función, en el canal LAB."""

def score_hypothesis(item):
    evidence = float(item.get("evidence", 0.0))
    falsifiability = float(item.get("falsifiability", 0.0))
    feasibility = float(item.get("feasibility", 0.0))
    importance = float(item.get("importance", 0.0))
    return max(0.0, min(100.0, 100.0 * (0.25 * evidence + 0.35 * falsifiability
                                        + 0.25 * feasibility + 0.15 * importance)))
