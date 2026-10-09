"""Local rehearsal, posts nothing, sends nothing. Classifies real comments from the tracker's state files and
(if ANTHROPIC_API_KEY is set) drafts a few flagged ones so the voice can be judged before deploy.
  ENV_FILE=../../.env python3 -m bot.dry_run [days] [n_drafts]"""
import glob
import json
import os
import sys

from . import classify, config

days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
n_drafts = int(sys.argv[2]) if len(sys.argv) > 2 else 5
import datetime as dt
since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")
root = os.path.join(os.path.dirname(__file__), "..", "comments", "state")

tiers = {"skip": [], "signoff": [], "auto": [], "flag": []}
flag_ctx = []
for f in sorted(glob.glob(os.path.join(root, "cmts_*.json"))):
    sc = os.path.basename(f)[5:-5]
    cs = json.load(open(f))
    for c in cs:
        if c["timestamp"] < since or (c.get("from") or {}).get("username") == config.IG_USERNAME:
            continue
        top = c.get("parent_id") or c["id"]
        thread = [x for x in cs if x["id"] == top or x.get("parent_id") == top]
        tier, reason, auto = classify.classify(c, thread)
        tiers[tier].append((c, reason, auto))
        if tier == "flag":
            flag_ctx.append((c, thread, sc))

total = sum(len(v) for v in tiers.values())
print(f"last {days} days: {total} comments")
for t, v in tiers.items():
    print(f"\n== {t} ({len(v)})")
    for c, reason, auto in v[:12]:
        u = (c.get("from") or {}).get("username", "?")
        print(f"   @{u}: {(c.get('text') or '')[:70]!r}  [{reason}]" + (f"  -> {auto}" if auto else ""))

if config.ANTHROPIC_KEY and flag_ctx:
    from . import draft
    print(f"\n== drafting {n_drafts} flagged comments with {config.DRAFT_MODEL}")
    for c, thread, sc in flag_ctx[-n_drafts:]:
        d = draft.draft(c, thread, "", sc)
        u = (c.get("from") or {}).get("username", "?")
        print(f"\n   @{u}: {(c.get('text') or '')[:120]}")
        print(f"   -> {d['reply']}" + ("   ⚠️ " + d["note"] if d["needs_marcus"] else ""))
else:
    print("\n(no ANTHROPIC_API_KEY, skipping drafts)")
