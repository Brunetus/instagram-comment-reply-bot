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


def rows(status=None, since=None):
    q, p = "SELECT * FROM comments WHERE 1=1", []
    if status:
        q += " AND status=?"; p.append(status)
    if since:
        q += " AND created_at>=?"; p.append(since)
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
