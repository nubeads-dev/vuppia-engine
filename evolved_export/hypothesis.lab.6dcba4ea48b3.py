def score_hypothesis(item):
    e = float(item.get("evidence", 0.0)); f = float(item.get("falsifiability", 0.0))
    g = float(item.get("feasibility", 0.0)); m = float(item.get("importance", 0.0))
    lin = 0.30*e + 0.30*f + 0.25*g + 0.15*m
    ex = 2.718281828459045**lin; exn = 2.718281828459045**(-lin)
    tanh = (ex-exn)/(ex+exn); sat = tanh/0.7615941559557649
    if sat<0.0: sat=0.0
    if sat>1.0: sat=1.0
    return max(0.0, min(100.0, 100.0*(0.9*lin+0.1*sat)))
