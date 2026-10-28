"""Prints a few findings of one class (argv[2]) outside tests from the queue (argv[1])."""
import json
import random
import sys

queue = json.load(open(sys.argv[1], encoding="utf-8"))
chosen = [r for r in queue if r["cls"] == sys.argv[2] and not r["path"].startswith("tests/")]
random.seed(7)
for record in random.sample(chosen, min(int(sys.argv[3]), len(chosen))):
    print(f'{record["path"]}:{record["line"]} [{record["function"]}] {record["condition"][:90]} -- {record["reason"][:110]}')
in_macro = sum(1 for r in queue if r["in_macro"])
print("in_macro", in_macro, "of", len(queue))
