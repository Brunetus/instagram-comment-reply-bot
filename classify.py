"""Deterministic tiering. Rules only, no model, so every decision is explainable in the digest.

  skip     tags, stickers, promos/spam, chats between other people -> no reply, digest only
  signoff  short closer on a thread Marcus already answered ("yeah", "got you man") -> no reply
  auto     emoji-only or a 1-4 word reaction -> emoji reply, posted without review; any other 1-2 word
           reaction -> the drafter writes a word-or-emoji reply, posted without review
  flag     everything else -> Sonnet drafts, Marcus approves on Telegram
"""
import re

from . import config

ME = config.IG_USERNAME

_EMOJI = re.compile(r"^[\s\U0001F000-\U0001FAFF☀-➿⬀-⯿❤️‍⃣!.,]+$")
# a real link, not a bare ".com" or "vercel.app" (those are the localhost reel's best comments)
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(com|io|dev|app|ai|co|xyz|in|live|org|net)/\S", re.I)
_MENTION = re.compile(r"@[\w.]+")
_SPAM = re.compile(r"contact us|dm me|dm for|follow @|check (out )?my (page|profile|bio)|link in bio|"
                   r"promo|discount|giveaway|gospel|jesus|repent|forex|crypto signal|whatsapp|telegram\.me|t\.me/", re.I)
_LAUGH = re.compile(r"\b(lol|lmao|lmfao|hehe|rofl)\b|(ha){2,}|😂|🤣|💀", re.I)
_FIRE = re.compile(r"\b(fire|facts?|true|fr+|goat|based|agreed?|exactly|yes+|yessir+|yup|yep|same|relatable|"
                   r"preach|absolutely|accurate|correct|legend|respect|king|peak|valid|let'?s go+)\b|🔥|🙌|👏|💯|🐐", re.I)
# only these emoji may be answered without review; anything else (🖕 👎 💩 🤡 🙄 🍆 ...) goes to Marcus
_GOOD_EMOJI = set("🔥🙌👏💯🐐💪❤️😍🥹😂🤣💀👍🫡✨🙏🥰😊😁😎🤝🫶💖💕🥳🎉😭😅😃😄😆🙂😉🤩👌🫠")
_EMOJI_NOISE = re.compile(r"[\s!.,️‍\U0001F3FB-\U0001F3FF❤]")   # spaces, punctuation, variation/skin/ZWJ


def _emoji_only_ok(s):
    """True if the comment is nothing but allow-listed emoji (plus spaces/punctuation)."""
    core = _EMOJI_NOISE.sub("", s)
    return bool(core) and all(ch in _GOOD_EMOJI for ch in core)
# a whole-comment reaction (these words are too ambiguous inside a longer sentence)
_WHOLE = {"real", "w", "bro", "fr", "frfr", "facts", "true", "same", "yes", "mood", "this", "relatable", "goat"}
_QUESTION = re.compile(r"\?|\b(how|why|what|which|where|when|who|can i|could you|should i|is it|is this|does it|do you)\b", re.I)
# negation / hostility: never auto-emoji these, a human decides
_NEG = re.compile(r"\b(not|no|nah|isn'?t|aren'?t|don'?t|doesn'?t|can'?t|won'?t|fake|scam|slop|cringe|wrong|nobody|cares|"
                  r"cap|lie|lies|lying|bs|bullshit|trash|mid|stop|cope|delusional|stupid|dumb|hate|worst|bad|dead|rip|"
                  r"job|loser|clown|bot|npc|idiot|cooked|touch grass|shut|ugly|broke|poor|lame|boring|sus|yikes|boomer|ragebait|bait|paid|ad|sponsored|shill|grift)\b", re.I)
_CLOSER = re.compile(r"\b(thanks?|thank you|thx|ty|yeah|yea|yes|yep|ok|okay|got (you|it)|sure( thing)?|will do|appreciate|"
                     r"same|true|fr|haha+|lol|amen|bro|man|noted|facts|real|cool|nice|ooh|wow|love it|agreed?)\b|🙌|❤️|👍|🫡|😂|🔥|👏", re.I)


# handles carrying a hate code (Grok/Marc, 2026-10-28, after an auto ❤️ went to "...1488"): never answered, never
# carded. Kept tight on purpose: a bare "88" is a birth year or a lucky number, so it only counts next to "14", "hh"
# or a clear marker, and words only count as whole tokens ("nazir", "siegfried", "whitler" stay clean).
_HATE_HANDLE = re.compile(
    r"(?<!\d)(?<!\d[._])(?:14[._]?88|1[._]4[._]8[._]8)(?!\d)"   # 1488, 14_88, 14.88, 1_4_8_8 (not 11.14.88 or 1.4.88)
    r"|(?<![a-z])h[._]?h[._]?88(?!\d)|(?<!\d)88[._]?hh(?![a-z])"         # hh88 / 88hh (heil hitler)
    r"|(?<![a-z\d])(?:ss|heil|reich|sieg)[._]?8[._]?8(?!\d)"              # ss88, heil88
    r"|(?<!\d)88[._]?(?:ss|heil|reich|sieg)(?![a-z])"
    r"|adolf[._]?hitler|hitler(?:jugend|youth)|zyklon"                   # glued forms ("adolfhitler88")
    r"|(?<![a-z])(?:sieg[._]?heil|heil[._]?hitler|hitler|nazis?|white[._]?(?:power|pride)[._]?(?:88)?|wpww|"
    r"aryan[._]?(?:nation|brotherhood|pride))(?![a-z])",
    re.I)


def hate_handle(username):
    """True if the handle matches a hate-code pattern. The bot neither replies to it nor sends a card."""
    return bool(_HATE_HANDLE.search(username or ""))


_ALNUM = re.compile(r"[A-Za-z0-9']+")


def _words(t):
    return [w for w in re.split(r"\s+", t.strip()) if w]


def classify(cm, thread):
    """cm: the comment dict. thread: list of comment dicts sharing the same top id (incl. cm), any order.
    Returns (tier, reason, auto_reply_text_or_None)."""
    text = (cm.get("text") or "").strip()
    user = (cm.get("from") or {}).get("username", "")
    if hate_handle(user):
        return "skip", "handle matches a hate-code pattern", None
    if not text:
        return "skip", "no text (sticker/gif)", None
    stripped = _MENTION.sub("", text).strip()
    mentions = _MENTION.findall(text)
    mentions_me = any(m.lower() == f"@{ME}".lower() for m in mentions)

    if _SPAM.search(text) or (_URL.search(text) and not _QUESTION.search(text)):
        return "skip", "promo/spam or bare link", None
    if mentions and not mentions_me and len(_words(stripped)) <= 4 and "?" not in stripped:
        return "skip", "friend tag", None

    is_reply = bool(cm.get("parent_id"))
    if is_reply and not mentions_me:
        # a reply aimed at someone else in the thread (people talking to each other)
        if mentions:
            return "skip", "reply to another commenter", None
    if is_reply:
        me_before = any((c.get("from") or {}).get("username") == ME and c["timestamp"] < cm["timestamp"]
                        for c in thread)
        if (me_before and len(_words(stripped)) <= 5 and "?" not in stripped and not _URL.search(stripped)
                and not re.search(r"\d|\$", stripped) and (_EMOJI.match(stripped) or _CLOSER.search(stripped))):
            return "signoff", "closer on a thread already answered", None

    w = _words(stripped)
    tier, reason, auto = "flag", "needs a real reply", None
    if _EMOJI.match(stripped) and re.search(r"[^\s!.,]", stripped):   # emoji only, not just "..."
        if not _emoji_only_ok(stripped):
            return "flag", "emoji outside the allow-list", None
        if re.search(r"🔥|🙌|👏|💯|🐐|💪", stripped) and not re.search(r"😂|🤣", stripped):
            tier, reason, auto = "auto", "emoji only", "🙌"
        else:
            tier, reason, auto = "auto", "emoji only", "❤️"
    elif (len(w) <= 4 and not _QUESTION.search(stripped) and not _NEG.search(stripped)
          and not re.search(r"\d|\$", stripped)):
        bare = re.sub(r"[^\w\s]", "", stripped).strip().lower()
        if _LAUGH.search(stripped) and len(w) <= 2:      # "lol true", "hahaha 😂"; longer laughs may carry a jab
            tier, reason, auto = "auto", "short laugh", "😂"
        elif _FIRE.search(stripped) or bare in _WHOLE:
            tier, reason, auto = "auto", "short reaction", "🙌"
        elif len(_ALNUM.findall(stripped)) <= 2 and _ALNUM.search(stripped) and not _URL.search(stripped):
            # any other one/two-word reaction ("Noted 😃", "Insane", "nicotine too"): the drafter writes a
            # word-or-emoji reply and it posts itself (Marcus, 2026-10-08), unless it carries an emoji we
            # would not answer blind
            core = _EMOJI_NOISE.sub("", _ALNUM.sub("", stripped))
            if not core or all(ch in _GOOD_EMOJI for ch in core):
                tier, reason, auto = "auto", "short reaction, drafted", None
    if tier == "auto" and is_reply and not mentions_me:
        # a reaction aimed at the top commenter, not at Marcus; an @mention from him there would be odd
        return "skip", "reply to another commenter", None
    return tier, reason, auto
