"""Builds history.json (per commenter: their past comments and Marcus's replies) from the chat-era comment
dumps in ../comments/state/cmts_*.json. Run from src/bot before `railway up`. history.json holds other people's
comments, so it is gitignored and only lives in the deployed image."""
import glob, json, os

here = os.path.dirname(os.path.abspath(__file__))
hist = {}
for f in glob.glob(os.path.join(here, "..", "comments", "state", "cmts_*.json")):
    sc = os.path.basename(f)[5:-5]
    d = json.load(open(f))
    items = d if isinstance(d, list) else d.get("data", [])
    by_top = {}
    for c in items:
        by_top.setdefault(c.get("parent_id") or c["id"], []).append(c)
    for top, cs in by_top.items():
        cs.sort(key=lambda c: c["timestamp"])
        for i, c in enumerate(cs):
            u = (c.get("from") or {}).get("username")
            if not u or u == "imabgold":
                continue
            mine = [x["text"] for x in cs[i + 1:] if (x.get("from") or {}).get("username") == "imabgold"]
            reply = next((t for t in mine if (t or "").lower().startswith(f"@{u.lower()}")), mine[0] if mine else None)
            if reply:   # the @handle is how Instagram threads it, not part of what Marcus said
                import re
                reply = re.sub(r"^@[\w.]+\s*", "", reply).strip()
            hist.setdefault(u, []).append({"ts": c["timestamp"], "shortcode": sc, "text": c.get("text") or "", "reply": reply})
for u in hist:
    hist[u].sort(key=lambda x: x["ts"])
json.dump(hist, open(os.path.join(here, "history.json"), "w"), ensure_ascii=False)
print(f"history.json: {len(hist)} commenters, {sum(len(v) for v in hist.values())} comments")
