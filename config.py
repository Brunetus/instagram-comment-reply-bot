"""Env-driven config. On Railway every value comes from the service's Variables tab.
Locally, set ENV_FILE=/path/to/.env (the project .env works) and the loader fills os.environ."""
import os


def _load_env_file(path):
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))


if os.environ.get("ENV_FILE"):
    _load_env_file(os.environ["ENV_FILE"])


def env(key, default=None, required=False):
    v = os.environ.get(key, default)
    if required and not v:
        raise SystemExit(f"missing env var {key}")
    return v


IG_TOKEN = env("IG_ACCESS_TOKEN", required=True)
IG_USER_ID = env("IG_USER_ID", required=True)
IG_USERNAME = env("IG_USERNAME", "imabgold")

TG_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = str(env("TELEGRAM_CHAT_ID", ""))

ANTHROPIC_KEY = env("ANTHROPIC_API_KEY", "")
DRAFT_MODEL = env("DRAFT_MODEL", "claude-opus-5-5")
DRAFT_EFFORT = env("DRAFT_EFFORT", "high")   # 2026-10-19: API credits are included with Max, so think harder
WEB_SEARCHES = int(env("WEB_SEARCHES", "2"))   # web searches the drafter may run per draft; 0 disables

DATA_DIR = env("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
POLL_SECONDS = int(env("POLL_SECONDS", "600"))
TZ = env("TZ", "America/Los_Angeles")
DIGEST_HOUR = int(env("DIGEST_HOUR", "9"))
AUTO_POST = env("AUTO_POST", "0") == "1"          # default shadow; set AUTO_POST=1 on Railway to post the auto tier
AUTO_DRAFT = env("AUTO_DRAFT", "0") == "1"        # 2026-10-25: drafts that pass the critic post themselves (Marcus)
MIN_AUTO_GAP = float(env("MIN_AUTO_GAP", "30"))   # seconds between two auto-posted drafts, the only volume guard
CATCHUP_SINCE = env("CATCHUP_SINCE", "2026-10-01")   # unanswered comments this recent get worked through after each poll
CATCHUP_PER_POLL = int(env("CATCHUP_PER_POLL", "3"))
CATCHUP_SECONDS = int(env("CATCHUP_SECONDS", "120"))   # wall-clock cap per poll so Telegram taps are never starved
NEWEST_N = int(env("NEWEST_N", "3"))
RECONCILE_EVERY = int(env("RECONCILE_EVERY", "6"))   # polls between open-card checks (6 x 10 min = hourly)
RECONCILE_MAX = int(env("RECONCILE_MAX", "10"))      # newest open cards checked per run
SWEEP_MINUTES = int(env("SWEEP_MINUTES", "60"))    # every reel on the account is swept this often (hot 7 are every POLL_SECONDS)
SWEEP_HOURS = int(env("SWEEP_HOURS", "48"))        # a sweep looks this far back on each reel
POST_GAP_SECONDS = float(env("POST_GAP_SECONDS", "4"))

# shortcode:label, same four as track_reels.py
# 2026-10-12 (Marcus): the localhost reel replaces "sexiest thing" (17 comments, quiet)
_FLAG_DEFAULT = ("DdQBUIRuSRM:one thing,Dd-Ss2PI0PB:localhost to .com,"
                 "DddHvD5OgIa:question for the men,DdxZCCmR8i0:greenest flag")
FLAGSHIP = dict(x.split(":", 1) for x in env("FLAGSHIP", _FLAG_DEFAULT).split(",") if x.strip())
