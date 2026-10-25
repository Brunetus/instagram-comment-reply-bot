"""SQLite state on the Railway volume. One row per comment ever seen; the status column is the audit trail."""
import os
import sqlite3
import time

from . import config

os.makedirs(config.DATA_DIR, exist_ok=True)
DB = os.path.join(config.DATA_DIR, "bot.db")

STATUSES = ("own", "preexisting", "skipped", "signoff", "auto_posted", "flagged",
            "approved_posted", "edited_posted", "declined", "answered_manually", "error", "shadow")

_schema = """
CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, shortcode TEXT, caption TEXT, ts TEXT, label TEXT, bootstrapped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS comments(
  id TEXT PRIMARY KEY, media_id TEXT, shortcode TEXT, username TEXT, text TEXT, ts TEXT, parent_id TEXT,
  tier TEXT, status TEXT, draft TEXT, needs_marcus INTEGER DEFAULT 0, reply_text TEXT, reply_id TEXT,
  tg_msg_id INTEGER, reason TEXT, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_comments_status ON comments(status);
CREATE INDEX IF NOT EXISTS ix_comments_tg ON comments(tg_msg_id);
CREATE TABLE IF NOT EXISTS authors(comment_id TEXT PRIMARY KEY, username TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def _conn():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.executescript(_schema)


def get_kv(k, default=None):
    with _conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_kv(k, v):
    with _conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def upsert_media(m, label):
    with _conn() as c:
        c.execute("""INSERT INTO media(id,shortcode,caption,ts,label) VALUES(?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET label=excluded.label, caption=excluded.caption""",
                  (m["id"], m["shortcode"], (m.get("caption") or "")[:400], m["timestamp"], label))


def media(media_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        return dict(r) if r else None


def media_bootstrapped(media_id):
    with _conn() as c:
        r = c.execute("SELECT bootstrapped FROM media WHERE id=?", (media_id,)).fetchone()
        return bool(r and r["bootstrapped"])


def mark_bootstrapped(media_id):
    with _conn() as c:
        c.execute("UPDATE media SET bootstrapped=1 WHERE id=?", (media_id,))


def seen(comment_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM comments WHERE id=?", (comment_id,)).fetchone() is not None


def add_comment(cm, media_id, shortcode, tier, status, **extra):
    now = time.time()
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO comments(id,media_id,shortcode,username,text,ts,parent_id,tier,status,
                     draft,needs_marcus,reply_text,reply_id,tg_msg_id,reason,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (cm["id"], media_id, shortcode, (cm.get("from") or {}).get("username", "?"),
                   cm.get("text") or "", cm.get("timestamp"), cm.get("parent_id"), tier, status,
                   extra.get("draft"), int(bool(extra.get("needs_marcus"))), extra.get("reply_text"),
                   extra.get("reply_id"), extra.get("tg_msg_id"), extra.get("reason"), now, now))


def update(comment_id, **fields):
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE comments SET {cols} WHERE id=?", (*fields.values(), comment_id))


def _d(row):
    """sqlite Row -> plain dict shaped like an API comment (adds `timestamp` and `from`), so the same
    helpers work on both. Returns None for None."""
    if row is None:
        return None
    d = dict(row)
    d["timestamp"] = d.get("ts")
    d["from"] = {"username": d.get("username")}
    return d


def get(comment_id):
    with _conn() as c:
        return _d(c.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone())


def by_tg_msg(msg_id):
    if msg_id is None:
        return None
    with _conn() as c:
        return _d(c.execute("SELECT * FROM comments WHERE tg_msg_id=?", (msg_id,)).fetchone())


def rows(status=None, since=None, by="updated_at"):
    """since: epoch seconds, compared against updated_at by default (the catch-up works on old rows)."""
    q, p = "SELECT * FROM comments WHERE 1=1", []
    if status:
        q += " AND status=?"; p.append(status)
    if since:
        q += f" AND {by}>=?"; p.append(since)
    q += " ORDER BY created_at"
    with _conn() as c:
        return [_d(r) for r in c.execute(q, p).fetchall()]


def cached_author(comment_id):
    with _conn() as c:
        r = c.execute("SELECT username FROM authors WHERE comment_id=?", (comment_id,)).fetchone()
        return r["username"] if r else None


def cache_author(comment_id, username):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO authors(comment_id,username) VALUES(?,?)", (comment_id, username))


# ---------- memory for the drafter (Marcus, 2026-10-19) ----------
_HANDLE = r"^@[\w.]+\s*"


def _strip_handle(t):
    import re as _re
    return _re.sub(_HANDLE, "", t or "").strip()


def by_user(username, limit=12):
    """This commenter's past comments (any reel) with Marcus's answer to each, oldest first. Marcus's replies are
    matched by username (so comments recorded before the bot existed count too): the first reply by him in the
    same thread after the comment that starts with @username, else the first one after it at all."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM comments WHERE username=? AND status NOT IN ('own','error','deleted') "
                         "ORDER BY ts DESC LIMIT ?", (username, limit)).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            reply = _strip_handle(r.get("reply_text")) if r.get("reply_text") else None
            if not reply:
                top = r.get("parent_id") or r["id"]
                mine = c.execute("SELECT text FROM comments WHERE username='imabgold' AND (parent_id=? OR id=?) AND ts>? "
                                 "ORDER BY ts", (top, top, r["ts"])).fetchall()
                hit = next((m["text"] for m in mine if (m["text"] or "").lower().startswith(f"@{username.lower()}")), None)
                reply = _strip_handle(hit or (mine[0]["text"] if mine else None)) or None
            pending = r.get("status") in ("new", "flagged") and not reply
            out.append({"id": r["id"], "ts": r["ts"], "shortcode": r.get("shortcode"), "text": r.get("text") or "",
                        "reply": reply, "pending": pending})
        return list(reversed(out))


def recent_replies(limit=30):
    """Marcus's latest replies: typed via Edit, approved drafts, and replies he posted by hand in the app
    (own rows that are NOT a reply the bot itself posted), each with the comment it answered. Newest first."""
    with _conn() as c:
        out = []
        for r in c.execute("SELECT username, text, reply_text, status, updated_at AS t FROM comments WHERE status IN "
                           "('edited_posted','approved_posted') AND reply_text IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
                           (limit,)).fetchall():
            out.append({"t": r["t"] or 0, "comment": r["text"] or "", "reply": _strip_handle(r["reply_text"]), "how": r["status"]})
        own = c.execute("""SELECT o.text AS reply, o.ts, o.created_at AS t,
                             (SELECT p.text FROM comments p WHERE p.username<>'imabgold' AND p.ts<o.ts
                                AND (p.parent_id=o.parent_id OR p.id=o.parent_id) ORDER BY p.ts DESC LIMIT 1) AS comment
                           FROM comments o WHERE o.status='own' AND o.parent_id IS NOT NULL
                             AND o.id NOT IN (SELECT reply_id FROM comments WHERE reply_id IS NOT NULL)
                           ORDER BY o.ts DESC LIMIT ?""", (limit,)).fetchall()
        out += [{"t": r["t"] or 0, "comment": r["comment"] or "", "reply": _strip_handle(r["reply"]), "how": "by hand"}
                for r in own if r["reply"]]
        out.sort(key=lambda x: -x["t"])
        return out[:limit]


def corrections(limit=12):
    """Cards where Marcus rewrote the draft (ignoring a leading @handle): (comment, drafted, posted)."""
    with _conn() as c:
        rows = c.execute("SELECT username, text, draft, reply_text FROM comments WHERE status='edited_posted' AND draft IS NOT NULL "
                         "AND reply_text IS NOT NULL ORDER BY updated_at DESC LIMIT ?", (limit * 2,)).fetchall()
        out = []
        for r in rows:
            d, p_ = _strip_handle(r["draft"]), _strip_handle(r["reply_text"])
            if d and p_ and d != p_:
                out.append({"comment": r["text"] or "", "draft": d, "posted": p_})
        return out[:limit]


def replies_on_media(media_id, limit=20):
    """The latest replies that went out under this reel (any path), newest first: the critic's repetition check."""
    with _conn() as c:
        rows = c.execute("SELECT reply_text FROM comments WHERE media_id=? AND reply_text IS NOT NULL AND status IN "
                         "('auto_posted','auto_drafted','approved_posted','edited_posted') ORDER BY updated_at DESC LIMIT ?",
                         (media_id, limit)).fetchall()
        return [_strip_handle(r["reply_text"]) for r in rows]


def thread_answered_by_hand(top_id):
    """True if Marcus himself (not the bot) has replied somewhere in this thread: he is handling it, keep it his."""
    with _conn() as c:
        r = c.execute("SELECT 1 FROM comments o WHERE o.username='imabgold' AND (o.parent_id=? OR o.id=?) "
                      "AND o.id NOT IN (SELECT reply_id FROM comments WHERE reply_id IS NOT NULL) LIMIT 1",
                      (top_id, top_id)).fetchone()
        return r is not None


def backlog(since, limit):
    """Comments the bot never answered: open cards and comments recorded at first sight, newest first."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM comments WHERE (status='preexisting' OR (status='flagged' AND needs_marcus=0)) "
                         "AND username<>'imabgold' AND ts>=? ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall()
        return [_d(r) for r in rows]


def is_bot_reply(reply_id):
    """True if this reply id is one the bot posted (any path): approved, edited, auto."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM comments WHERE reply_id=? LIMIT 1", (reply_id,)).fetchone() is not None

