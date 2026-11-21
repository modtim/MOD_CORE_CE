# Generates CORE/Worker/internal/executor/testdata/gpu_placement_cases.json
# from the Python source of truth (services.gpu_sharing). Run in my_fastapi_app.
import json, sys
sys.path.insert(0, "/app")
from services.gpu_sharing import choose_gpu_placement, parse_demand

vrams = [None, 5, 10, 12.5, 20, 35, 40, 60]
demands = [None, {}]
for v in vrams:
    demands.append({"cuda": [v]})
    demands.append({"cuda": [v], "shareable": True})
demands += [{"cuda": [8, 16]}, {"gpu": [8, None], "shareable": True}, {"": [30]}]
capses = [None, {"modes": ["whole"]},
          {"modes": ["time_slice"], "gpu_memory_gb": 80},
          {"modes": ["time_slice"], "time_slice_replicas": 8, "gpu_memory_gb": 48},
          {"modes": ["time_slice"]},
          {"modes": ["mig"]},
          {"modes": ["mig"], "mig_profiles": ["2g.20gb", "1g.10gb"]},
          {"modes": ["mig", "time_slice"], "mig_profiles": ["3g.40gb"], "gpu_memory_gb": 24}]
cases = []
for d in demands:
    count, vram, shareable = parse_demand(d)
    for c in capses:
        for pref in ("auto", "whole", "time_slice", "mig"):
            p = choose_gpu_placement(d, cluster_caps=c, prefer=pref)
            cases.append({
                "demand_raw": d,
                "demand": {"count": count, "vram_gb": vram or 0, "shareable": shareable},
                "caps": c, "prefer": pref,
                "expected": {"mode": p.mode, "count": p.count, "mig_profile": p.mig_profile or "",
                             "resources": p.resources, "node_selector": p.node_selector,
                             "reason": p.reason}})
json.dump({"generator": "python services/gpu_sharing.choose_gpu_placement (W21)",
           "cases": cases}, sys.stdout, indent=1, sort_keys=True, ensure_ascii=False)
