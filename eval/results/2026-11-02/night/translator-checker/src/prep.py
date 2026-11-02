#!/usr/bin/env python3
"""eval-gemma prep (CPU, no model): build the Part A and Part B item lists, guard the references, check imports."""
import collections

from common_gemma import HERE, a_items, b_items, common, write

import bench_a  # noqa: F401  (import check: raum.pipeline.translate etc.)
import bench_b  # noqa: F401

A = a_items()
B = b_items()
g = common.Guard()
red = [(it["sid"], it["src_lang"], it["tgt_lang"], g.check(it["room"], it["src"], it["src_lang"], it["ref"], it["tgt_lang"]).mismatches)
       for it in A]
red = [r for r in red if r[3]]
print("A items", len(A), collections.Counter((i["src_lang"], i["tgt_lang"]) for i in A))
print("A guard red on references:", len(red))
for r in red:
    print("   ", r)
print("B items", len(B), collections.Counter(i["kind"] for i in B), collections.Counter(i["perturbation"] for i in B if i["kind"] == "pert"))
print("B items without baseline:", sum(1 for i in B if not i["base"]))
write(HERE / "out" / "a-items.json", A)
write(HERE / "out" / "b-items.json", B)
