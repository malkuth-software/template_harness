"""Write gui_forms' loop-branch decisions for one area from the first-run queue."""
import importlib, json, sys
area_name, module_name, out = sys.argv[1], sys.argv[2], sys.argv[3]
from ctx_sites import sites
mod = importlib.import_module(module_name)
listed = sites(area_name)
try:
    document = json.load(open(out, encoding='utf-8'))
except FileNotFoundError:
    document = {"about": "", "decisions": []}
document["about"] = ("Decisions on the loop branches that harness/check_loop_branches.py (E2) reports, matched by path, "
                     "function and condition text. keep: the branch stays, for the reason given. false_shape: the "
                     "condition is not invariant (it changes inside the loop through a call, a callback or another thread).")
kept = [d for d in document["decisions"] if not any(d["path"] == b["path"] for b in listed)]
for number, (decision, reason) in sorted(mod.DECISIONS.items()):
    b = listed[number - 1]
    kept.append({"path": b["path"], "function": b["function"], "condition": b["condition"],
                 "decision": decision, "reason": reason})
seen = set()
unique = []
for d in sorted(kept, key=lambda d: (d["path"], d["function"], d["condition"])):
    key = (d["path"], d["function"], d["condition"])
    if key in seen:
        continue
    seen.add(key)
    unique.append(d)
document["decisions"] = unique
with open(out, "w", encoding="utf-8", newline="\n") as stream:
    json.dump(document, stream, indent=2, ensure_ascii=False)
    stream.write("\n")
print(len(unique), "decisions")
