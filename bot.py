"""@imabgold comment bot. One always-on process:
  every POLL_SECONDS: pull new comments on the 3 newest reels + 4 flagships, tier them, auto-reply the easy ones,
                      send the rest to Marcus on Telegram with a Sonnet draft and Approve/Edit/Skip buttons
  continuously:       long-poll Telegram for his taps and edits
  every SWEEP_MINUTES: every other reel on the account (newest comments page, SWEEP_HOURS back)
  daily:              a digest at DIGEST_HOUR

Invariants (also the audit brief):
  * Graph API only. Never hides, deletes, DMs, likes or follows. Never touches a browser session.
  * Nothing is posted that Marcus did not approve, except the auto tier (emoji / one-word reactions) when AUTO_POST=1.
  * Right before ANY post, the thread is re-fetched; if Marcus already replied in the app, the bot stands down.
    An author the API will not name is treated as Marcus (stand down). A failed fetch stands down.
  * One reply per comment, ever. A POST is never retried. Every decision lands in sqlite with a status.
  * Content rules are enforced in code on every outgoing text (em dash, "zero coding", DM promises).
"""
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
import traceback
import zoneinfo

from . import classify, config, draft, ig, state, tg

ME = config.IG_USERNAME
_LOCAL = zoneinfo.ZoneInfo(config.TZ)


def log(*a):
    print(dt.datetime.now(_LOCAL).strftime("%m-%d %H:%M:%S"), *a, flush=True)


def _ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")


def _u16(s):
    """Telegram's 4096 limit counts UTF-16 code units (an emoji is 2)."""
    return len(s.encode("utf-16-le")) // 2


# ---------- content rules (invariant: enforced in code, not just in the prompt) ----------
_FORBIDDEN = re.compile(r"—|zero cod(e|ing)|no coding background|can'?t (read |write )?code|cannot code|non-? ?technical|"
                        r"never written a line|don'?t (know how to )?code|i'?ll (dm|send|email|message)|dm'?e?d you|dm me|dm you|"
                        r"check your dm|sent you|sending you|message me|inbox me|link below|link in bio|"
                        r"\b(i will|i can|let me|gonna|going to)\s+(dm|send|email|message|share|drop|shoot)\b|"
                        r"i'?ll (share|drop|shoot|post the link)|\bin (your|the) dms?\b", re.I)


def content_violation(text):
    norm = text.replace("’", "'").replace("‘", "'").replace("–", "-")
    m = _FORBIDDEN.search(norm)
    return m.group(0) if m else None


# ---------- media ----------
def tracked_media():
    """NEWEST_N most recent non-flagship reels + every flagship, with a short label each."""
    allm = ig.media(limit=config.NEWEST_N + 25)
    out = []
    for m in allm:
        if m["shortcode"] not in config.FLAGSHIP and len([x for x in out if not x[1]]) < config.NEWEST_N:
            out.append((m, None))
    for m in allm:
        if m["shortcode"] in config.FLAGSHIP:
            out.append((m, config.FLAGSHIP[m["shortcode"]]))
    res = []
    for m, lab in out:
        label = lab or (m.get("caption") or m["shortcode"]).split("\n")[0][:40]
        state.upsert_media(m, label)
        res.append((m, label))
    return res


# ---------- threads & the double-reply guard ----------
def _author(c):
    """Username, or None when the API will not say. Only real names are cached."""
    u = (c.get("from") or {}).get("username")
    if u:
        return u
    u = state.cached_author(c["id"])
    if u:
        return u
    u = ig.author(c["id"])
    if u:
        state.cache_author(c["id"], u)
    return u


_THREAD_CACHE = {}     # top_id -> (fetched_at, items): read paths reuse a thread for 5 min
_NODE_COOLDOWN = {}    # top_id -> until: Instagram rate-limits a single hot comment node; leave it alone a while


def _node_limited(e):
    return "rate-limits on the node" in str(e) or "1349210" in str(e)


def thread_of(top_id, fresh=False):
    """Top comment + all replies, each with a resolved author (or '?'), sorted by time.
    fresh=False may reuse a fetch from the last 5 minutes (drafting, reconcile, catch-up); the post guard asks fresh."""
    now = time.time()
    hit = _THREAD_CACHE.get(top_id)
    if not fresh and hit and now - hit[0] < 300:
        return [dict(c) for c in hit[1]]
    if _NODE_COOLDOWN.get(top_id, 0) > now:
        raise ig.IGError(f"node {top_id} cooling down after Instagram rate-limits on the node")
    try:
        top = ig.comment(top_id)
        items = [top]
        for r in ig.replies(top_id):
            r["parent_id"] = top_id
            r["from"] = {"username": _author(r) or "?"}
            items.append(r)
    except ig.IGError as e:
        if _node_limited(e):
            _NODE_COOLDOWN[top_id] = now + 1800
        raise
    items = sorted(items, key=lambda c: c["timestamp"])
    _THREAD_CACHE[top_id] = (now, items)
    return [dict(c) for c in items]


def _answered_via_media(cm):
    """Fallback when the comment node itself is rate-limited: read the newest comments of the REEL (a different node)
    plus what the poll has stored. True/False, or None if that fails too."""
    top_id = cm.get("parent_id") or cm["id"]
    media_id = cm.get("media_id")
    if not media_id:
        return None
    try:
        recent = ig.comments(media_id, max_pages=3)
    except ig.IGError as e:
        log("guard fallback failed:", str(e)[:120])
        return None
    for c in recent + state.thread_rows(top_id):
        if c["id"] == cm["id"] or (c.get("parent_id") or c["id"]) != top_id:
            continue
        a = (c.get("from") or {}).get("username")
        if a in (ME, "?", None) and (not cm.get("parent_id") or (c.get("timestamp") or "") > cm["timestamp"]):
            return True
    return False


def already_answered(cm):
    """True  = Marcus (or this bot) already replied after this comment in its thread, stand down.
       False = clear to post.
       None  = could not verify (API error / deleted comment), stand down.
    Re-fetched live every time, never from cache."""
    top_id = cm.get("parent_id") or cm["id"]
    try:
        thread = thread_of(top_id, fresh=True)
    except ig.IGError as e:
        _LAST_GUARD_ERR[0] = str(e)
        if _node_limited(e):   # only this comment is throttled: verify through the reel instead
            v = _answered_via_media(cm)
            log(f"guard: node limited, checked via the reel instead -> {v}")
            return v
        log("guard: could not fetch thread:", str(e)[:160])
        return None
    for c in thread:
        if c["id"] == cm["id"]:
            continue
        a = (c.get("from") or {}).get("username")
        if a not in (ME, "?", None):
            continue
        # a == ME, or unknown (treated as Marcus: fail closed)
        if not cm.get("parent_id") or c["timestamp"] > cm["timestamp"]:
            return True
    return False


# ---------- posting ----------
def _handle_of(cm):
    cm = dict(cm)
    return cm.get("username") or (cm.get("from") or {}).get("username") or ""


def _drop_hate_handle(cm):
    """A hate-code handle (classify.hate_handle): settle the row as skipped and take any card down. True if dropped."""
    if not classify.hate_handle(_handle_of(cm)):
        return False
    cid = dict(cm)["id"]
    row = state.get(cid) or {}
    if state.update_if(cid, state.OPEN, status="skipped", tier="skip", reason="handle matches a hate-code pattern") \
            and row.get("tg_msg_id") and not tg.delete(row["tg_msg_id"]):
        try:
            tg.edit(row["tg_msg_id"], "⏭ skipped: handle matches a hate-code pattern")
        except Exception as e:
            log("hate-handle card edit failed", repr(e))
    log(f"skip: @{_handle_of(cm)} (handle matches a hate-code pattern)")
    return True


def post_reply(cm, text, status):
    """Posts `text` for comment `cm` (API dict or state dict). Returns the reply id, or None if it stood down.
    Serialised across threads: a tap and the catch-up may hold the same comment, so the row is re-read under the lock
    and anything no longer open (posted, declined, answered in the app, deleted) is left alone."""
    with _POST_LOCK:
        cur = state.get(dict(cm)["id"])
        if cur and cur["status"] not in state.OPEN:
            log(f"stand down (already {cur['status']}): @{cur['username']} {cur['id']}")
            return None
        if status.startswith("auto") and _drop_hate_handle(cm):
            return None   # backstop: the classifier already skips these; Marcus's own taps still go through
        pe = _pending_edit() if status.startswith("auto") else None
        if pe and pe.get("cid") == dict(cm)["id"]:   # Marcus tapped Edit on this one: his text is coming
            log(f"stand down (Edit armed): @{dict(cm)['username']}")
            return None
        rid = _post_reply(cm, text, status)
    if rid:
        time.sleep(config.POST_GAP_SECONDS)   # pacing between posts, outside the lock so a tap never waits on it
    return rid


def _post_reply(cm, text, status):
    cm = dict(cm)
    bad = content_violation(text)
    if bad:
        state.update(cm["id"], status="flagged", reason=f"blocked: contains '{bad}'")
        log(f"blocked reply to @{cm['username']} (contains '{bad}')")
        return None
    if ig.comments_blocked():
        _queue_retry(cm["id"], text, status)
        state.update(cm["id"], status="flagged", reason="queued: Instagram has paused replies for the account for a "
                                                         "while, the bot will post it automatically when it clears")
        return None
    verdict = already_answered(cm)
    if verdict is None:
        # transient: keep the card live so Marcus can simply tap again
        why = ("Instagram is throttling reads right now, try again in 10-15 min" if _rate_limited(_LAST_GUARD_ERR[0])
               else "could not verify the thread (Instagram glitch), tap Approve again")
        state.update(cm["id"], status="flagged", reason=why)
        return None
    if verdict:
        state.update(cm["id"], status="answered_manually", reason="Marcus replied in the app first")
        log(f"stand down (already answered): @{cm['username']} {cm['id']}")
        return None
    top_id = cm.get("parent_id") or cm["id"]
    msg = text
    if cm.get("parent_id") and not text.lower().startswith(f"@{cm['username']}".lower()):
        msg = f"@{cm['username']} {text}"
    try:
        rid = ig.reply(top_id, msg)
    except ig.IGError as e:
        if _rate_limited(e):
            # Instagram refused the POST outright (403 throttle), so nothing landed: queue it, the loop retries it
            _queue_retry(cm["id"], text, status)
            state.update(cm["id"], status="flagged", reason="queued: Instagram has paused replies for the account for a "
                                                             "while, the bot will post it automatically when it clears")
            log(f"queued (throttled) -> @{cm['username']}: {msg[:60]}")
            return None
        if str(e).startswith("HTTP 4"):   # definitive refusal, nothing posted
            state.update(cm["id"], status="flagged", reason=f"Instagram refused the reply: {str(e)[:120]}")
            return None
        state.update(cm["id"], status="error", reason=f"post outcome unknown, not retried: {str(e)[:120]}")
        raise   # a timeout may have landed: never retry, surface it
    state.update(cm["id"], status=status, reply_text=msg, reply_id=rid)
    state.set_kv(f"retry:{cm['id']}", "")
    log(f"posted [{status}] -> @{cm['username']}: {msg[:80]}")
    return rid


def _queue_retry(cid, text, status):
    raw = state.get_kv(f"retry:{cid}")
    try:
        prev = json.loads(raw) if raw else {}
    except ValueError:
        prev = {}
    tries = int(prev.get("tries", 0)) + 1
    state.set_kv(f"retry:{cid}", json.dumps({"text": text, "status": status, "tries": tries,
                                             "at": time.time() + min(20 * 60 * tries, 2 * 3600)}))


def retry_queued(limit=5):
    """Replies Instagram throttled: post them when their time comes (same guard as any post). Gives up after 8 tries."""
    n = 0
    for k, raw in state.kv_prefix("retry:"):
        if n >= limit or not raw:
            continue
        try:
            job = json.loads(raw)
        except ValueError:
            state.set_kv(k, ""); continue
        if job.get("at", 0) > time.time():
            continue
        cid = k.split(":", 1)[1]
        row = state.get(cid)
        if not row or row["status"] != "flagged":
            state.set_kv(k, ""); continue
        if int(job.get("tries", 0)) >= 8:
            state.set_kv(k, "")
            if row.get("tg_msg_id"):
                tg.send(f"⚠️ gave up on @{row['username']} after 8 tries, Instagram kept throttling. Tap Approve again later.",
                        reply_to=row["tg_msg_id"])
            continue
        n += 1
        try:
            rid = post_reply(row, job["text"], job["status"])
        except Exception as e:
            log("retry failed", repr(e)[:120]); state.set_kv(k, ""); continue
        if rid and row.get("tg_msg_id"):
            icon = "✏️ posted your text" if job["status"] == "edited_posted" else "✅ posted"
            if job["status"] == "auto_drafted":
                icon = "🤖 posted"
            tg.edit(row["tg_msg_id"], f"{icon} to @{row['username']}: {job['text']} (after Instagram's throttle cleared)")
    return n


# ---------- telegram cards ----------
def _card(cm, thread, label, d):
    head = f"💬 {label} · @{cm['username']}"
    tail = (([f"🌐 In English: {d['translation']}"] if d.get("translation") else [])
            + ([f"🧠 {d['explainer']}"] if d.get("explainer") else []) + [f"Draft: {d['reply']}"])
    if d.get("needs_marcus"):
        tail.append(f"⚠️ check: {d.get('note') or 'the draft may need a fact only you know'}")
    tail.append("Tap a button. Edit = then just type your text as the next message.")
    tail_txt = "\n\n" + "\n".join(tail)
    budget = 3800 - _u16(head) - _u16(tail_txt)
    lines = []
    for c in thread:
        u = (c.get("from") or {}).get("username", "?")
        t = (c.get("text") or "").strip().replace("\n", " ")
        if len(t) > 200:
            t = t[:197] + "..."
        lines.append(("➡️ " if c["id"] == cm["id"] else "   ") + f"@{u}: {t}")
    # keep the target line and as much context as fits; drop the oldest lines first
    tgt = next((i for i, c in enumerate(thread) if c["id"] == cm["id"]), len(lines) - 1)
    keep = lines[:]
    while keep and _u16("\n".join(keep)) > budget and len(keep) > 1:
        drop = 0 if tgt > 0 else 1   # never drop the target line
        keep.pop(drop)
        tgt = max(0, tgt - 1) if drop == 0 else tgt
    body = "\n".join(keep)
    if len(keep) < len(lines):
        body = "   (…earlier replies trimmed)\n" + body
    return head + "\n" + body + tail_txt


_BUTTONS = [("✅ Approve", "ap"), ("✏️ Edit", "ed"), ("⏭ Skip", "sk")]
_LEAD = re.compile(r"^\W*(money)\W*$", re.I)   # comment keywords that mean "send me the thing": always Marcus's
# slurs, sexual content, self-harm: a human decides these, never the critic alone. Mild swearing ("shit", "damn"),
# "sexy" (the reels' own premise) and plain negation ("not", "no", "nah") go to the critic, which judges tone
# (2026-10-26: the old word list held 19 of 31 friendly comments for words like "not alone" and "bro cooked").
_CRUDE = classify._CRUDE
_LAST_AUTO = [0.0]
_LAST_GUARD_ERR = [""]
POSTED = ("approved_posted", "edited_posted", "auto_posted", "auto_drafted")
_POST_LOCK = threading.RLock()   # the Telegram thread (taps, typed replies) and the Instagram worker never post at once
_SWEEP_NOW = threading.Event()   # /sweep asks the worker for a full-account pass right away


def _posted_card(cm, thread, label, d, posted, why, edit_msg_id=None):
    """The same card as a flag, but the bottom reads 🤖 posted. Replaces the old card when one exists."""
    dd = dict(d); dd["reply"] = posted; dd["needs_marcus"] = False
    txt = _card(cm, thread, label, dd).replace(f"Draft: {posted}", f"🤖 posted: {posted}") \
        .replace("\nTap a button. Edit = then just type your text as the next message.", f"\n({why})")
    try:
        if edit_msg_id:
            tg.edit(edit_msg_id, txt)
            return edit_msg_id
        return tg.send(txt)
    except Exception as e:
        log("posted card failed", repr(e))
        return None


def auto_or_flag(row, cm, thread, m, label, edit_msg_id=None):
    """The flag tier since 2026-10-25: draft, let the critic judge, post on its own or hand Marcus a card."""
    caption = m.get("caption") or ""
    d = draft.draft(cm, thread, caption, label)
    text = cm.get("text") or ""
    top_id = cm.get("parent_id") or cm["id"]
    if not config.AUTO_DRAFT:   # tier off: plain card, NOT marked as a hold, so it is caught up once the tier is on
        if not edit_msg_id:
            flag_to_marcus(row, thread, label, caption, pre=d)
        else:
            state.update_if(cm["id"], state.OPEN, status="flagged")
        return
    stripped = classify._MENTION.sub("", text)
    by_hand = any((c.get("from") or {}).get("username") in (ME, "?", None) and c["id"] != cm["id"]
                  and not state.is_bot_reply(c["id"]) for c in thread)   # a reply of his the bot did not post
    hold = None
    if d["needs_marcus"]:
        hold = f"needs you: {d.get('note') or 'a fact only you know'}"
    elif _LEAD.match(text):
        hold = "lead keyword"
    elif row.get("reason") == "emoji outside the allow-list":
        hold = "emoji outside the allow-list"
    elif _CRUDE.search(stripped):
        hold = "slur or sexual wording"
    elif by_hand or state.thread_answered_by_hand(top_id):
        hold = "you are handling this thread yourself"
    elif "@" in d["reply"]:
        hold = "draft tags someone"
    pe = _pending_edit()
    if pe and pe.get("cid") == cm["id"]:   # Marcus tapped Edit on this one, his text is coming: leave it to him
        return
    if hold:
        if edit_msg_id:   # an old card: keep it, it already has buttons
            state.update_if(cm["id"], state.OPEN, status="flagged", needs_marcus=1, reason=hold)
            return
        flag_to_marcus(row, thread, label, caption, pre=d, note=f"({hold})")
        return
    if ig.comments_blocked():   # can't post now: queue the drafted reply through the critic later, card for now
        if edit_msg_id:   # the card is already there: never send a second one
            state.update_if(cm["id"], state.OPEN, needs_marcus=0, reason="Instagram limit, drafted while paused")
            return
        flag_to_marcus(row, thread, label, caption, pre=d)
        state.update_if(cm["id"], state.OPEN, needs_marcus=0, reason="Instagram limit, drafted while paused")
        return
    v = draft.critique(cm, thread, d["reply"], caption, label, state.replies_on_media(m["id"]))
    if v["verdict"] == "flag":
        if edit_msg_id:
            state.update_if(cm["id"], state.OPEN, status="flagged", needs_marcus=1, reason=f"critic: {v['reason']}")
            return
        flag_to_marcus(row, thread, label, caption, pre=d, note=f"(critic: {v['reason']})")
        return
    wait = config.MIN_AUTO_GAP - (time.time() - _LAST_AUTO[0])
    if wait > 0:
        time.sleep(wait)
    rid = post_reply(row, v["reply"], "auto_drafted")
    _LAST_AUTO[0] = time.time()
    if rid:
        why = "critic rewrote the draft" if v["verdict"] == "rewrite" else "critic approved the draft"
        mid = _posted_card(row, thread, label, d, v["reply"], why, edit_msg_id=edit_msg_id)
        if mid:
            state.update(cm["id"], tg_msg_id=mid)
        return
    after = state.get(cm["id"])
    if after and after["status"] == "flagged" and not edit_msg_id:   # blocked text or a glitch: a human decides
        flag_to_marcus(row, thread, label, caption, pre=d, note=f"({after.get('reason') or 'could not post'})")


def flag_to_marcus(cm, thread, label, caption, note=None, pre=None):
    if _drop_hate_handle(cm):   # never a card for these, whichever path got here (lead keyword included)
        return
    d = pre or draft.draft(cm, thread, caption, label)   # pre: a draft already made for this comment
    if note:
        label = f"{label} {note}"
    msg_id = tg.send(_card(cm, thread, label, d), buttons=[[(l, f"{code}:{cm['id']}") for l, code in _BUTTONS]])
    if not state.update_if(cm["id"], state.OPEN, status="flagged", draft=d["reply"], tg_msg_id=msg_id,
                           needs_marcus=int(bool(d["needs_marcus"] or note)),   # a note = a hold: the catch-up must not redraft it
                           reason=(note or d.get("note"))):
        tg.delete(msg_id)   # settled meanwhile (Marcus's tap won): no card
        log(f"card dropped, @{cm['username']} was settled while drafting")
        return
    log(f"flagged -> card {msg_id}: @{cm['username']} \"{(cm.get('text') or '')[:50]}\" draft: {d['reply'][:60]}")


# ---------- the 10-minute poll ----------
def process_comment(cm, m, label, fetched):
    """fetched: all comments pulled for this media this round (thread context)."""
    if state.seen(cm["id"]):
        return
    user = (cm.get("from") or {}).get("username", "?")
    if user == ME:
        state.add_comment(cm, m["id"], m["shortcode"], "own", "own")
        return
    top_id = cm.get("parent_id") or cm["id"]
    thread = [c for c in fetched if c["id"] == top_id or c.get("parent_id") == top_id]
    if cm.get("parent_id") and not any(c["id"] == top_id for c in thread):
        try:
            thread = thread_of(top_id)
        except ig.IGError:
            pass
    if not any(c["id"] == cm["id"] for c in thread):   # the replies edge can lag a brand-new reply
        thread.append(cm)
    thread.sort(key=lambda c: c["timestamp"])          # API order is newest-first; cards and drafts read oldest-first
    tier, reason, auto_text = classify.classify(cm, thread)
    if tier in ("skip", "signoff"):
        state.add_comment(cm, m["id"], m["shortcode"], tier, "skipped" if tier == "skip" else "signoff", reason=reason)
        log(f"{tier}: @{user} ({reason}) {(cm.get('text') or '')[:50]}")
        return
    state.add_comment(cm, m["id"], m["shortcode"], tier, "new", reason=reason)
    row = state.get(cm["id"])
    if _LEAD.match(cm.get("text") or ""):   # "money" etc: a lead, never an emoji, always Marcus (2026-10-10 lesson)
        flag_to_marcus(row, thread, label, m.get("caption") or "", note="(🎯 lead keyword, DM them the guide by hand)")
        state.update(cm["id"], tier="flag", needs_marcus=1, reason="lead keyword")   # after the card: flag_to_marcus resets needs_marcus
        return
    if tier == "auto" and auto_text is None:   # short reaction: the drafter picks the word or emoji
        d = draft.draft(cm, thread, m.get("caption") or "", label)
        if d["needs_marcus"] or len(re.findall(r"[A-Za-z0-9']+", d["reply"])) > 3 or "?" in d["reply"] or "@" in d["reply"]:
            # drafter failed, wants a human, wrote a sentence or tagged someone: fail closed, card instead of post
            state.update(cm["id"], tier="flag", reason=f"short draft: {d['note'][:80] or 'too long to auto-post'}")
            flag_to_marcus(state.get(cm["id"]), thread, label, m.get("caption") or "", pre=d)
            return
        auto_text = d["reply"]
    if tier == "auto":
        if config.AUTO_POST:
            post_reply(row, auto_text, "auto_posted")
        else:
            state.update(cm["id"], reply_text=auto_text)
            flag_to_marcus(row, thread, label, m.get("caption") or "", note=f"(shadow: would auto-post {auto_text})")
        return
    auto_or_flag(row, cm, thread, m, label)


def catchup_unanswered():
    """After each poll (Marcus, 2026-10-25): work through comments the bot never answered, newest first, a few at a
    time: open cards and comments recorded at first sight since CATCHUP_SINCE. Each one is checked live first."""
    n = 0
    t0 = time.time()
    for row in state.backlog(config.CATCHUP_SINCE, config.CATCHUP_PER_POLL):
        if time.time() - t0 > config.CATCHUP_SECONDS:
            break
        state.set_kv("worker_beat", time.time())
        pe = _pending_edit()
        if pe and pe.get("cid") == row["id"]:   # Marcus is typing a reply for this one right now
            continue
        try:
            n += _catchup_one(row)
        except RateLimited:
            log("catchup: Instagram rate limit hit, resuming next poll"); break
        except Exception as e:
            log(f"catchup: error on @{row['username']} {row['id']}: {e!r}")
            state.update_if(row["id"], state.OPEN, status="error", reason=f"catchup: {str(e)[:150]}")
    if n:
        log(f"catchup: worked through {n} unanswered comments")
    return n


class RateLimited(Exception):
    pass


def _catchup_one(row):
    """One backlog row. Returns 1 when it was acted on, 0 when it was only re-filed."""
    cm = {"id": row["id"], "text": row["text"], "timestamp": row["timestamp"],
          "from": {"username": row["username"]}, "parent_id": row.get("parent_id")}
    top_id = row.get("parent_id") or row["id"]
    if _drop_hate_handle(row):
        return 0
    # cheap pre-pass on what the db already knows: closers and tags are filed without an API call
    pre_tier, pre_reason, _ = classify.classify(cm, state.thread_rows(top_id) + [cm])
    if pre_tier in ("skip", "signoff") and not _LEAD.match(cm.get("text") or ""):
        if state.update_if(row["id"], state.OPEN, status="skipped" if pre_tier == "skip" else "signoff", tier=pre_tier, reason=pre_reason) and row.get("tg_msg_id"):
            tg.delete(row["tg_msg_id"]) or tg.edit(row["tg_msg_id"], f"⏭ {pre_reason}: @{row['username']}")
        return 0
    if True:
        try:
            thread = thread_of(top_id)
        except ig.IGError as e:
            if "does not exist" in str(e):
                if state.update_if(row["id"], state.OPEN, status="deleted", reason="comment gone") and row.get("tg_msg_id"):
                    tg.delete(row["tg_msg_id"])
            else:
                log(f"catchup: skipping @{row['username']}: {str(e)[:120]}")
                if _rate_limited(e) and not _node_limited(e):   # a throttled single comment just waits its cooldown
                    raise RateLimited()
            return 0
        if any((c.get("from") or {}).get("username") == ME and c["timestamp"] > row["timestamp"] for c in thread):
            if state.update_if(row["id"], state.OPEN, status="answered_manually", reason="already answered") and row.get("tg_msg_id"):
                tg.delete(row["tg_msg_id"]) or tg.edit(row["tg_msg_id"], f"✋ already answered @{row['username']}")
            return 0
        if not any(c["id"] == cm["id"] for c in thread):
            thread.append(cm); thread.sort(key=lambda c: c["timestamp"])
        md = state.media(row["media_id"]) or {}
        m = {"id": row["media_id"], "shortcode": row["shortcode"], "caption": md.get("caption") or ""}
        label = md.get("label") or row["shortcode"]
        if _LEAD.match(cm.get("text") or ""):
            if not row.get("tg_msg_id"):
                flag_to_marcus(state.get(row["id"]), thread, label, m["caption"], note="(🎯 lead keyword, DM them the guide by hand)")
            state.update_if(row["id"], state.OPEN, status="flagged", tier="flag", needs_marcus=1, reason="lead keyword")
            return 0
        tier, reason, auto_text = classify.classify(cm, thread)
        if tier in ("skip", "signoff"):
            if state.update_if(row["id"], state.OPEN, status="skipped" if tier == "skip" else "signoff", tier=tier, reason=reason) and row.get("tg_msg_id"):
                tg.delete(row["tg_msg_id"]) or tg.edit(row["tg_msg_id"], f"⏭ {reason}: @{row['username']}")
            return 0
        state.update_if(row["id"], state.OPEN, tier=tier, reason=reason)   # status stays as it is: a card stays tappable
        fresh = state.get(row["id"])
        if tier == "auto":
            if auto_text is None:
                d = draft.draft(cm, thread, m["caption"], label)
                if d["needs_marcus"] or len(re.findall(r"[A-Za-z0-9']+", d["reply"])) > 3 or "?" in d["reply"] or "@" in d["reply"]:
                    if not row.get("tg_msg_id"):
                        flag_to_marcus(state.get(row["id"]), thread, label, m["caption"], pre=d)
                    state.update_if(row["id"], state.OPEN, status="flagged", tier="flag", needs_marcus=1, reason="short draft needs a look")
                    return 0
                auto_text = d["reply"]
            if not config.AUTO_POST:   # shadow mode: never post the auto tier from the backlog either
                state.update_if(row["id"], state.OPEN, status="flagged", needs_marcus=1, reason="auto tier in shadow")
                return 0
            rid = post_reply(fresh, auto_text, "auto_posted")
            if rid and row.get("tg_msg_id"):
                tg.edit(row["tg_msg_id"], f"🤖 posted to @{row['username']}: {auto_text}")
            elif not rid and not row.get("tg_msg_id") and (state.get(row["id"]) or {}).get("status") == "flagged":
                flag_to_marcus(state.get(row["id"]), thread, label, m["caption"], note="(could not auto-post)")
        else:
            auto_or_flag(fresh, cm, thread, m, label, edit_msg_id=row.get("tg_msg_id"))
            if state.get(row["id"])["status"] == "preexisting":   # nothing posted and no card made: a card next time
                state.update_if(row["id"], ("preexisting",), status="flagged")
        return 1


def _rate_limited(e):
    return "request limit" in str(e).lower() or '"code":4,' in str(e) or '"code": 4,' in str(e)


def reconcile_open_cards():
    """Every poll (Marcus, 2026-10-10): any open card whose comment he has since answered in the app is taken
    off the Telegram queue, so a tap can never double-reply. Same live thread check as before a post."""
    n = 0
    if int(state.get_kv("poll_no", "0")) % 3:   # every third poll (30 min): the tap-time guard still protects every post
        return 0
    week = time.time() - 7 * 86400
    for row in state.rows(status="flagged"):
        if (row.get("created_at") or 0) < week:   # old cards are not worth two API calls every half hour
            continue
        if not row.get("tg_msg_id"):   # no card to take down; post_reply re-checks the thread before any post
            continue
        try:
            verdict = already_answered(row)
        except Exception as e:
            log("reconcile: stopped:", repr(e)[:120]); break
        if verdict is None and _rate_limited(_LAST_GUARD_ERR[0]) and not _node_limited(_LAST_GUARD_ERR[0]):
            log("reconcile: Instagram rate limit hit, resuming next time"); break
        if verdict is None:
            # the commenter deleted it? then there is nothing to answer, drop the card
            try:
                ig.comment(row["id"])
                continue   # exists, just a transient fetch error: leave the card
            except ig.IGError as e:
                if "does not exist" not in str(e):
                    continue
            if state.update_if(row["id"], state.OPEN, status="deleted", reason="comment deleted by its author") and row.get("tg_msg_id"):
                tg.delete(row["tg_msg_id"]) or tg.edit(row["tg_msg_id"], f"🗑 @{row['username']} deleted their comment")
            log(f"reconcile: @{row['username']} deleted their comment, card removed")
            continue
        if verdict is not True:
            continue
        if not state.update_if(row["id"], state.OPEN, status="answered_manually", reason="Marcus replied in the app"):
            continue   # settled by a tap meanwhile: its card already shows the outcome
        n += 1
        if not row.get("tg_msg_id"):
            continue
        if not tg.delete(row["tg_msg_id"]):   # older than 48h: collapse the card instead
            try:
                tg.edit(row["tg_msg_id"], f"✋ you already answered @{row['username']} in the app")
            except Exception as e:
                log("reconcile: card edit failed", repr(e))
        log(f"reconcile: @{row['username']} answered in the app, card removed")
    return n


BACKFILL = os.path.join(os.path.dirname(__file__), "backfill.json")


def load_backfill():
    """One-time, at startup: replies Marcus approved for comments the bot had skipped. backfill.json is gitignored
    (other people's comments) and ships in the image only. Each entry is queued on the retry queue, so it posts
    through post_reply's live thread check, 5 per poll, and is recorded as the bot's own reply."""
    if not os.path.exists(BACKFILL):
        return 0
    raw = open(BACKFILL, "rb").read()
    tag = hashlib.sha1(raw).hexdigest()[:12]
    if state.get_kv("backfill_loaded") == tag:
        return 0
    state.set_kv("backfill_loaded", tag)
    n = 0
    for e in json.loads(raw):
        if e.get("action") != "post" or not e.get("cid") or not e.get("reply"):
            continue
        if content_violation(e["reply"]):
            log(f"backfill: refused @{e.get('user')} ({content_violation(e['reply'])})"); continue
        if not state.update_if(e["cid"], ("skipped", "signoff"), status="flagged", draft=e["reply"], needs_marcus=1,
                               reason="backfill: reply Marcus approved for a comment the bot had skipped"):
            continue   # not in the db, or already answered some other way
        state.set_kv(f"retry:{e['cid']}", json.dumps({"text": e["reply"], "status": "approved_posted", "tries": 0,
                                                      "at": time.time()}))
        n += 1
    log(f"backfill: queued {n} approved replies")
    return n


def settle_short_cards():
    """One-time pass at startup (Marcus, 2026-10-08): open cards whose comment now qualifies as a short
    reaction post themselves, same thread re-check as any post. Anything else stays a card."""
    n = 0
    for row in state.rows(status="flagged"):
        cm = {"id": row["id"], "text": row["text"], "timestamp": row["timestamp"],
              "from": {"username": row["username"]}, "parent_id": row.get("parent_id")}
        try:
            thread = thread_of(row.get("parent_id") or row["id"])
        except ig.IGError as e:
            log(f"settle: skipping @{row['username']}: {e}")
            continue
        tier, reason, auto = classify.classify(cm, thread)
        if tier != "auto":
            continue
        md = state.media(row["media_id"]) or {}
        if auto is None:
            d = draft.draft(cm, thread, md.get("caption") or "", md.get("label") or row["shortcode"])
            if d["needs_marcus"] or len(re.findall(r"[A-Za-z0-9']+", d["reply"])) > 3 or "?" in d["reply"] or "@" in d["reply"]:
                continue
            auto = d["reply"]
        rid = post_reply(row, auto, "auto_posted")
        if rid:
            n += 1
            try:
                tg.edit(row["tg_msg_id"], f"🤖 posted to @{row['username']}: {auto}")
            except Exception as e:
                log("settle: card edit failed", repr(e))
    if n:
        log(f"settle: {n} open short cards posted")
        tg.send(f"🤖 cleared {n} open card(s) that are one-word reactions, posted short replies.")
    return n


def _bootstrap(m, label, fetched):
    n = 0
    for c in fetched:
        if not state.seen(c["id"]):
            state.add_comment(c, m["id"], m["shortcode"], "preexisting", "preexisting"); n += 1
    state.mark_bootstrapped(m["id"])
    log(f"bootstrapped {label}: {n} existing comments recorded, none acted on")


def poll_once():
    seen_new = 0
    for m, label in tracked_media():
        try:
            if not state.media_bootstrapped(m["id"]):
                # first sight of a reel: record everything already there and do NOT act on it
                _bootstrap(m, label, ig.comments(m["id"], max_pages=None))
                continue
            fetched = ig.comments(m["id"], max_pages=2,
                                  stop=lambda items: bool(items) and any(state.seen(c["id"]) for c in items))   # page 2 only on a burst
        except ig.IGError as e:
            log(f"poll: skipping {label} this round: {e}")
            continue
        for c in sorted(fetched, key=lambda c: c["timestamp"]):
            if state.seen(c["id"]):
                continue
            seen_new += 1
            try:
                process_comment(c, m, label, fetched)
            except Exception as e:
                if state.seen(c["id"]):
                    state.update(c["id"], status="error", reason=str(e)[:200])
                else:
                    state.add_comment(c, m["id"], m["shortcode"], "?", "error", reason=str(e)[:200])
                log("error on comment", c["id"], repr(e)); traceback.print_exc()
    log(f"poll done, {seen_new} new comments")


def sweep():
    """Every SWEEP_MINUTES: EVERY reel on the account, comments from the last SWEEP_HOURS (newest page first,
    stops as soon as a page is all old or all seen). Same bootstrap + process path as the 10-minute poll."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=config.SWEEP_HOURS)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    tracked = {m["id"] for m, _ in tracked_media()}
    n = 0
    for m in ig.all_media():
        if m["id"] in tracked:
            continue   # the hot 7 are polled every POLL_SECONDS already
        label = (m.get("caption") or m["shortcode"]).split("\n")[0][:40]
        state.set_kv("worker_beat", time.time())
        try:
            state.upsert_media(m, label)
            stop = lambda items: bool(items) and all(c["timestamp"] < since or state.seen(c["id"]) for c in items)
            fetched = ig.comments(m["id"], max_pages=4, stop=stop)
            if not state.media_bootstrapped(m["id"]):
                _bootstrap(m, label, fetched)
                continue
        except ig.IGError as e:
            log(f"sweep: skipping {label}: {e}")
            continue
        for c in sorted(fetched, key=lambda c: c["timestamp"]):
            if c["timestamp"] < since or state.seen(c["id"]):
                continue
            try:
                process_comment(c, m, label, fetched); n += 1
            except Exception as e:
                if state.seen(c["id"]):
                    state.update(c["id"], status="error", reason=str(e)[:200])
                else:
                    state.add_comment(c, m["id"], m["shortcode"], "?", "error", reason=str(e)[:200])
                log("sweep error on comment", c["id"], repr(e))
    log(f"sweep done, {n} new comments handled")
    return n


# ---------- telegram handling ----------
HELP = ("/status  followers + tracked reels + today's counts\n/digest  the full daily report, on demand\n"
        "/pause  stop reading and auto-posting (your taps and typed replies still post)\n/resume\n"
        "/sweep  full-account catch-up now\n/reconcile  drop open cards you already answered in the app\n/cancel  forget the card you tapped Edit on\n/help")


def _paused():
    return state.get_kv("paused", "0") == "1"


def _status_text():
    f = ig.followers()
    lines = [f"Followers: {f:,}", ""]
    for m, label in tracked_media():
        try:
            i = ig.insights(m["id"])
            lines.append(f"{label}: {i.get('views',0):,} views · {i.get('likes',0):,} likes · "
                         f"{i.get('shares',0):,} shares · {i.get('saved',0):,} saves · {m.get('comments_count',0):,} cmts")
        except ig.IGError:
            lines.append(f"{label}: insights unavailable")
    day = time.time() - 86400
    c = {s: len(state.rows(status=s, since=day)) for s in
         ("auto_posted", "auto_drafted", "flagged", "approved_posted", "edited_posted", "declined", "skipped", "signoff",
          "answered_manually", "error")}
    waiting = len(state.rows(status="flagged"))
    lines += ["", f"Last 24h: auto emoji {c['auto_posted']} · auto drafts {c['auto_drafted']} · approved {c['approved_posted']} · edited {c['edited_posted']} · "
                  f"declined {c['declined']} · skipped {c['skipped']+c['signoff']} · you beat me to {c['answered_manually']}"
                  + (f" · errors {c['error']}" if c['error'] else ""),
              f"Waiting on you: {waiting} · Auto-post: {'ON' if config.AUTO_POST else 'shadow'} · Paused: {_paused()}"]
    beat = float(state.get_kv("worker_beat", "0") or 0)
    if beat:
        lines.append(f"Instagram side last ticked {int(time.time() - beat) // 60} min ago")
    return "\n".join(lines)


def _resolve_card(msg):
    """Which card does this text message answer? Direct reply to the card, or reply to the Edit prompt."""
    ref = msg.get("reply_to_message") or {}
    rid = ref.get("message_id")
    row = state.by_tg_msg(rid)
    if row:
        return row
    cid = state.get_kv(f"prompt:{rid}") if rid else None
    return state.get(cid) if cid else None


EDIT_TTL = 30 * 60   # an armed Edit expires after 30 min so a stray message later never posts
_HINT = "\n\n✏️ Type your reply for @{u} as your next message."
_HINT_RE = re.compile(r"(\n\n✏️ Type your reply for @\S+ as your next message\.)+$")


def _buttons_for(cid):
    return [[(l, f"{code}:{cid}") for l, code in _BUTTONS]]


def _pending_edit():
    raw = state.get_kv("pending_edit")
    try:
        pe = json.loads(raw) if raw else None
    except ValueError:
        pe = None
    if not isinstance(pe, dict) or not all(k in pe for k in ("cid", "msg_id", "text", "ts")):
        if raw:
            state.set_kv("pending_edit", "")
        return None
    return pe


def _disarm_edit():
    """Clear the armed card and put its text back if it is still waiting. Returns False if the restore failed."""
    pe = _pending_edit()
    if not pe:
        return True
    state.set_kv("pending_edit", "")
    row = state.get(pe["cid"])
    if not (row and row["status"] == "flagged" and pe["text"]):
        return True
    try:
        tg.edit(pe["msg_id"], pe["text"], buttons=_buttons_for(pe["cid"]))
        return True
    except Exception as e:
        log("restore card failed", repr(e))
        return False


def _arm_edit(row, card_text):
    """Edit tap: the next plain message Marcus sends is his reply for this card. Only one card is armed at a
    time; arming another restores the previous card first, and nothing is armed if that restore fails."""
    if not _disarm_edit():
        tg.send("⚠️ could not reset the previous Edit, tap Edit again.")
        return
    base = _HINT_RE.sub("", card_text or "")
    if base:   # a card we can edit: show the hint on the card itself
        tg.edit(row["tg_msg_id"], base + _HINT.format(u=row["username"]), buttons=_buttons_for(row["id"]))
    else:      # Telegram gave us no text for the card (very old message): arm without touching it
        tg.send(f"✏️ Type your reply for @{row['username']} as your next message.")
    state.set_kv("pending_edit", json.dumps({"cid": row["id"], "msg_id": row["tg_msg_id"], "text": base,
                                             "ts": time.time()}))


def _armed_card():
    pe = _pending_edit()
    if not pe:
        return None
    if time.time() - pe["ts"] > EDIT_TTL:
        _disarm_edit()
        return None
    row = state.get(pe["cid"])
    return row if row and row["status"] == "flagged" else None


def handle_update(u):
    if not tg.from_owner(u):
        return
    cb = u.get("callback_query")
    if cb:
        code, _, cid = (cb.get("data") or "").partition(":")
        row = state.get(cid) if cid else None
        live = bool(row and row["status"] == "flagged")
        try:   # answer first: Telegram rejects answers older than a few seconds and we may be behind
            tg.answer_callback(cb["id"], "" if live else "already handled")
        except Exception:
            pass
        if not live:
            return
        with _POST_LOCK:   # the worker may be posting this very row: one of us settles it, the other sees that
            _handle_tap(code, cid, row, cb)
        return
    _handle_text(u)


def _handle_tap(code, cid, row, cb):
    if code == "ed":
        _arm_edit(row, (cb.get("message") or {}).get("text") or "")
        return
    _disarm_edit()   # any other tap ends an armed Edit, on this card or another
    if code == "ap":
        rid = post_reply(row, row["draft"], "approved_posted")
        after = state.get(cid)
        if rid:
            tg.edit(row["tg_msg_id"], f"✅ posted to @{row['username']}: {row['draft']}")
        elif after["status"] in POSTED:   # the bot posted it a moment before the tap landed
            tg.edit(row["tg_msg_id"], f"🤖 posted to @{row['username']}: {after['reply_text']}")
        elif after["status"] == "flagged":   # transient glitch or blocked text: keep the buttons
            icon = "⏳" if (after.get("reason") or "").startswith("queued") else "⚠️ not posted to"
            wid = tg.send(f"{icon} @{row['username']}: {after['reason']}", reply_to=row["tg_msg_id"])
            state.set_kv(f"prompt:{wid}", cid)   # replying to the warning still targets this card
        else:
            tg.edit(row["tg_msg_id"], f"↩️ did not post to @{row['username']}: {after['reason']}")
    elif code == "sk":
        if state.update_if(cid, state.OPEN, status="declined"):
            tg.edit(row["tg_msg_id"], f"⏭ skipped @{row['username']}: {(row['text'] or '')[:80]}")
        else:
            after = state.get(cid)
            if after["status"] in POSTED:
                tg.edit(row["tg_msg_id"], f"🤖 posted to @{row['username']}: {after['reply_text']}")
            else:
                tg.edit(row["tg_msg_id"], f"⏭ @{row['username']} was already settled ({after['status'].replace('_', ' ')})")


def _handle_text(u):
    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text or msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date"):
        return   # forwarded text is never a reply
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/pause":
            state.set_kv("paused", "1"); tg.send("⏸ paused. No reading or auto-posting until /resume. Your taps and typed replies still post.")
        elif cmd == "/resume":
            state.set_kv("paused", "0"); tg.send("▶️ resumed.")
        elif cmd == "/reconcile":
            n = reconcile_open_cards(); tg.send(f"✋ {n} card(s) removed, you had answered those in the app." if n else "nothing to remove")
        elif cmd == "/cancel":
            tg.send("✖ edit cancelled" if _pending_edit() else "nothing to cancel"); _disarm_edit()
        elif cmd == "/status":
            tg.send(_status_text())
        elif cmd == "/digest":
            digest()
        elif cmd == "/sweep":
            if _paused():
                tg.send("paused, /resume first")
            else:
                _SWEEP_NOW.set(); tg.send("sweeping every reel in the background, I'll report when it's done")
        else:
            tg.send(HELP)
        return
    # plain text in reply to a card (or to the Edit prompt) = Marcus's own words, post them
    row = _resolve_card(msg)
    if msg.get("reply_to_message") and not row:
        tg.send("That isn't a comment card. Tap Edit on the card you mean, then type your text.")
        return
    row = row or _armed_card()
    if not row:
        tg.send("Tap Edit on a card first (or reply directly to the card), then type your text. /help")
        return
    with _POST_LOCK:
        _disarm_edit()   # consumed: put the armed card back before the outcome edit below
        row = state.get(row["id"])
        if row["status"] != "flagged":
            tg.send(f"@{row['username']} was already handled ({row['status']}).")
            return
        clean = text.replace("—", ",")
        rid = post_reply(row, clean, "edited_posted")
        after = state.get(row["id"])
    if rid:
        tg.edit(row["tg_msg_id"], f"✏️ posted your text to @{row['username']}: {clean}")
    elif after["status"] in POSTED:
        tg.edit(row["tg_msg_id"], f"🤖 posted to @{row['username']}: {after['reply_text']}")
    elif after["status"] == "flagged":   # blocked wording or a glitch: card stays live, try again
        icon = "⏳" if (after.get("reason") or "").startswith("queued") else "⚠️ not posted:"
        wid = tg.send(f"{icon} {after['reason']}", reply_to=row["tg_msg_id"])
        state.set_kv(f"prompt:{wid}", row["id"])
    else:
        tg.edit(row["tg_msg_id"], f"↩️ did not post to @{row['username']}: {after['reason']}")


def digest():
    day = time.time() - 86400
    auto = state.rows(status="auto_posted", since=day)
    skipped = state.rows(status="skipped", since=day) + state.rows(status="signoff", since=day)
    errors = state.rows(status="error", since=day)
    waiting = state.rows(status="flagged")
    lines = ["☀️ Daily digest", "", _status_text(), ""]
    drafted = state.rows(status="auto_drafted", since=day)
    if drafted:
        lines.append(f"Auto-drafted and posted ({len(drafted)}):")
        lines += [f"  @{r['username']}: \"{(r['text'] or '')[:40]}\" → {r['reply_text']}" for r in drafted[-40:]]
    if auto:
        lines.append(f"Auto-posted ({len(auto)}):")
        lines += [f"  @{r['username']}: \"{(r['text'] or '')[:40]}\" → {r['reply_text']}" for r in auto[-25:]]
    if waiting:
        lines.append(f"\nStill waiting on you ({len(waiting)}), scroll up for the cards.")
    if skipped:
        lines.append(f"\nSkipped, no reply ({len(skipped)}):")
        lines += [f"  @{r['username']}: \"{(r['text'] or '')[:40]}\" ({r['reason']})" for r in skipped[-20:]]
    if errors:
        lines.append(f"\nErrors ({len(errors)}), I could not handle these:")
        lines += [f"  @{r['username']}: {r['reason']}" for r in errors[-10:]]
    tg.send("\n".join(lines))


# ---------- main loop ----------
def main():
    for k in ("TG_TOKEN", "TG_CHAT", "ANTHROPIC_KEY"):
        if not getattr(config, k):
            raise SystemExit(f"missing {k}; set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and ANTHROPIC_API_KEY")
    state.init()
    try:
        _disarm_edit()   # never carry an armed Edit across a restart
    except Exception as e:
        log("startup disarm failed", repr(e))
    log(f"bot up · auto_post={config.AUTO_POST} · auto_draft={config.AUTO_DRAFT} · model={config.DRAFT_MODEL} · tz={config.TZ}")
    if state.get_kv("requeued_neg_holds") != "2026-10-26":
        state.set_kv("requeued_neg_holds", "2026-10-26")
        n = state.requeue(reason_like="%negative, hostile or crude wording%")
        log(f"re-queued {n} cards held by the old negative-word list")
    try:
        load_backfill()
    except Exception as e:
        log("backfill load failed", repr(e))
    if state.get_kv("dropped_hate_handles") != "2026-10-28":
        state.set_kv("dropped_hate_handles", "2026-10-28")
        try:
            n = sum(_drop_hate_handle(r) for st in state.OPEN for r in state.rows(status=st))
            log(f"dropped {n} open rows from hate-code handles")
        except Exception as e:
            log("hate-handle pass failed", repr(e))
    if config.AUTO_POST and state.get_kv("settled_short_cards") != "2026-10-08":
        state.set_kv("settled_short_cards", "2026-10-08")   # once, even if the pass itself errors out
        try:
            settle_short_cards()
        except Exception as e:
            log("settle failed", repr(e)); traceback.print_exc()
    tg.send(f"🤖 bot online · auto-post {'ON' if config.AUTO_POST else 'shadow'} · polling every {config.POLL_SECONDS//60} min\n{HELP}")
    worker = threading.Thread(target=_ig_worker, name="instagram", daemon=True)
    worker.start()
    offset = int(state.get_kv("tg_offset", "0"))
    errors = 0
    while True:   # Telegram only: taps and typed replies are answered within a second, whatever Instagram is doing
        try:
            if not worker.is_alive():   # the worker catches everything itself; this is the belt to its braces
                log("instagram worker died, restarting it")
                worker = threading.Thread(target=_ig_worker, name="instagram", daemon=True)
                worker.start()
            beat = float(state.get_kv("worker_beat", "0") or 0)
            if beat and time.time() - beat > 1800 and state.get_kv("beat_alerted") != str(int(beat)):
                state.set_kv("beat_alerted", str(int(beat)))
                tg.send(f"⚠️ the Instagram side has not ticked for {int(time.time() - beat) // 60} min, it may be stuck")
            for u in tg.get_updates(offset):
                offset = u["update_id"] + 1
                state.set_kv("tg_offset", offset)
                try:
                    handle_update(u)
                except Exception as e:
                    log("telegram handler error", repr(e)); traceback.print_exc()
                    tg.send(f"⚠️ error handling that: {type(e).__name__}: {str(e)[:150]}")
            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            errors += 1
            log("telegram loop error", repr(e)); traceback.print_exc()
            time.sleep(min(60 * errors, 600))


def _ig_worker():
    """The Instagram side on its own thread: poll, retry queue, catch-up, sweep, digest. Nothing here ever makes a
    Telegram tap wait; posting itself is serialised with the taps through _POST_LOCK in post_reply."""
    next_poll = 0.0
    next_sweep = time.time() + 120   # first full-account sweep 2 min after start (bootstraps every reel, acts on none)
    errors = 0
    state.set_kv("worker_beat", time.time())
    while True:
        try:
            # 1) Instagram poll
            if time.time() >= next_poll:
                next_poll = time.time() + config.POLL_SECONDS
                if not _paused():
                    state.set_kv("poll_no", int(state.get_kv("poll_no", "0")) + 1)
                    poll_once()
                    if not ig.comments_blocked():
                        reconcile_open_cards()
                        retry_queued()
                        catchup_unanswered()
                    else:
                        log(f"comment calls paused by Instagram's limit, resuming in {int(ig._BLOCK['until'] - time.time())//60} min")
            # 2) whole-account sweep every SWEEP_MINUTES, or now on /sweep
            if _SWEEP_NOW.is_set() or time.time() >= next_sweep:
                asked = _SWEEP_NOW.is_set()
                _SWEEP_NOW.clear()
                next_sweep = time.time() + config.SWEEP_MINUTES * 60
                if _paused():
                    if asked:
                        tg.send("sweep skipped, the bot is paused")
                else:
                    try:
                        n = sweep()
                    except Exception as e:
                        if asked:
                            tg.send(f"sweep failed: {type(e).__name__}: {str(e)[:150]}")
                        raise
                    if asked:
                        tg.send(f"sweep done, {n} new comments handled")
            # 3) daily digest
            now = dt.datetime.now(_LOCAL)
            today = now.strftime("%Y-%m-%d")
            if config.DIGEST_HOUR >= 0 and now.hour >= config.DIGEST_HOUR and state.get_kv("last_digest") != today:
                state.set_kv("last_digest", today); digest()
            errors = 0
            state.set_kv("worker_beat", time.time())
            _SWEEP_NOW.wait(5)   # idle in 5 s steps; a /sweep wakes it at once
        except Exception as e:
            errors += 1
            log("loop error", repr(e)); traceback.print_exc()
            if errors == 2:
                try:
                    tg.send(f"🚨 two errors in a row, still running: {type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass
            _SWEEP_NOW.wait(min(60 * errors, 600))


if __name__ == "__main__":
    main()
