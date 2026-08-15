#!/usr/bin/env python3
"""Render data/new_roles.json as a markdown issue body for notifications."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
roles = json.loads((ROOT / "data" / "new_roles.json").read_text())

by_cat = {}
for r in roles:
    by_cat.setdefault(r["category"], []).append(r)

CAP = 30  # keep bulk expansions (coverage improvements) from emailing a wall of text

lines = [f"**{len(roles)} new matching role(s)** just appeared on the tracked VC job boards.", ""]
if len(roles) > CAP:
    lines.append(f"_Large batch — likely expanded coverage rather than {len(roles)} same-day "
                 f"postings. Showing {CAP}; the rest are on the dashboard with NEW badges._")
    lines.append("")
shown = 0
for cat in sorted(by_cat):
    if shown >= CAP:
        break
    lines.append(f"### {cat}")
    lines.append("")
    for r in sorted(by_cat[cat], key=lambda x: x["company"].lower()):
        if shown >= CAP:
            break
        loc = ", ".join(r["locations"]) or "location n/a"
        lines.append(f"- [{r['title']} — {r['company']}]({r['url']})  \n"
                     f"  {r['metro']} · {loc} · via {r['board_name']}")
        shown += 1
    lines.append("")
lines.append("---")
lines.append("Open the [dashboard](../../deployments) (GitHub Pages) to review, filter, and mark applications.")
print("\n".join(lines))
