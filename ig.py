"""Instagram Graph API, read + reply only. Every call is retried 3x. Nothing here hides or deletes."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = "https://graph.instagram.com/v23.0"


class IGError(Exception):
    pass


# Circuit breaker (2026-10-26): when Instagram throttles comment objects ("Application request limit reached",
# code 4), EVERY comment-node call fails app-wide, and each further attempt keeps the window full. So after the first
# throttle, comment-node calls are not attempted at all for BLOCK_SECONDS; reel-level edges keep working.
BLOCK_SECONDS = 45 * 60
_BLOCK = {"until": 0.0}


def comments_blocked():
    return time.time() < _BLOCK["until"]


def _is_throttle(msg):
    return '"code":4' in msg.replace(" ", "") or "request limit reached" in msg.lower()


def _call(path, params, data=None):
    params = dict(params or {})
    node_call = not path.startswith(str(config.IG_USER_ID)) and "/insights" not in path and "/comments" not in path
    if node_call and comments_blocked():
        raise IGError(f"HTTP 403 on {path}: comment calls paused, Instagram request limit reached (breaker)")
    params["access_token"] = config.IG_TOKEN
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data else None
    # A POST is NEVER retried: a timed-out reply may still have landed, and a retry would post it twice.
    # The caller (post_reply) re-checks the thread before any later attempt.
    tries = 1 if body else 3
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, data=body, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            last = IGError(f"HTTP {e.code} on {path}: {msg}")
            if node_call and _is_throttle(msg):
                _BLOCK["until"] = time.time() + BLOCK_SECONDS
            if e.code in (400, 403, 404):   # not transient (or a rate limit): retrying only deepens it
                raise last
        except Exception as e:  # timeouts, connection resets
            last = IGError(f"{type(e).__name__} on {path}: {e}")
        if i < tries - 1:
            time.sleep(2 * (i + 1))
    raise last


def _paginate(path, params, max_pages=None, stop=None):
    """Yield items page by page. `stop(items)` returning True ends pagination early."""
    url_params = dict(params)
    page = 0
    j = _call(path, url_params)
    while True:
        items = j.get("data", [])
        yield items
        page += 1
        if stop and stop(items):
            return
        nxt = j.get("paging", {}).get("next")
        if not nxt or (max_pages and page >= max_pages):
            return
        # paging.next is a full URL with the token already embedded
        for i in range(3):
            try:
                with urllib.request.urlopen(nxt, timeout=25) as r:
                    j = json.load(r)
                break
            except Exception as e:
                if i == 2:
                    raise IGError(f"paging failed on {path}: {e}")
                time.sleep(2 * (i + 1))


def followers():
    return _call(config.IG_USER_ID, {"fields": "followers_count"}).get("followers_count", 0)


def media(limit=30):
    return _call(f"{config.IG_USER_ID}/media",
                 {"fields": "id,shortcode,caption,timestamp,comments_count", "limit": limit}).get("data", [])


def all_media():
    out = []
    for page in _paginate(f"{config.IG_USER_ID}/media",
                          {"fields": "id,shortcode,caption,timestamp,comments_count", "limit": 50}):
        out += page
    return out


COMMENT_FIELDS = "id,text,from{username},timestamp,parent_id"


def comments(media_id, max_pages=2, stop=None):
    """Newest first. Includes replies (they carry parent_id = the TOP comment, always)."""
    out = []
    for page in _paginate(f"{media_id}/comments", {"fields": COMMENT_FIELDS, "limit": 50},
                          max_pages=max_pages, stop=stop):
        out += page
    return out


def insights(media_id):
    j = _call(f"{media_id}/insights", {"metric": "views,reach,likes,comments,shares,saved"})
    return {x["name"]: x["values"][0]["value"] for x in j.get("data", [])}


def comment(cid):
    return _call(cid, {"fields": COMMENT_FIELDS})


def replies(top_id):
    """ALL replies under a top-level comment (paginated). NOTE: `from` is NOT returned on this edge; use author()."""
    out = []
    for page in _paginate(f"{top_id}/replies", {"fields": "id,text,timestamp", "limit": 50}):
        out += page
    return out


def author(cid):
    """Username of a comment's author, or None if the API does not say (never guess)."""
    return (_call(cid, {"fields": "from{username}"}).get("from") or {}).get("username") or None


def reply(top_id, message):
    """Post a reply. Instagram only allows replies on TOP-level comments; thread replies get an @mention."""
    return _call(f"{top_id}/replies", {}, data={"message": message}).get("id")
