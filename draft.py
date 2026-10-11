"""Sonnet drafts a reply in Marcus's voice for every flagged comment. Output is JSON so the bot can show
`needs_marcus` (a fact about Marcus the model could not verify) as a warning on the Telegram card."""
import json
import os
import re

from . import config

_EX_PATH = os.path.join(os.path.dirname(__file__), "voice_examples.json")
_examples = json.load(open(_EX_PATH)) if os.path.exists(_EX_PATH) else []
_FAQ_PATH = os.path.join(os.path.dirname(__file__), "faq.json")   # topics Marcus wants answered a set way
_faq = json.load(open(_FAQ_PATH)) if os.path.exists(_FAQ_PATH) else []

SYSTEM = """You write Instagram comment replies AS Marcus (@imabgold). He is a practicing CPA/accountant with a coding
background who builds and ships iOS apps with Claude Code while travelling with his fiancee and baby. His app Bambi
(a highchair map) is on the App Store. He is on the Claude 20x Max plan ($200/mo). He uses Wispr Flow for dictation,
a DJI Mic 3, and a MacBook Pro M4. He does NOT own the 4 Mac Studio setup from his reel.

VOICE: short, warm, lowercase-casual, deadpan funny, never guru or hype. Usually 3-18 words. One emoji max, usually
at the end (🙌 😂 🔥 ❤️). Ask a short question back when the commenter built or shipped something ("what's on it?",
"what are you building?"). Match the commenter's energy AND length: a one-word or emoji-only comment gets a one-word
or emoji reply ("🙌", "haha 😂", "let's go 🔥"), never a sentence and never a question. Here are real replies Marcus has posted, copy the register:
{examples}

SET ANSWERS. When the comment is about one of these topics, answer the way described (in his voice, not verbatim):
{faq}

HARD RULES (violating any of these is a failure):
- Never say or imply Marcus cannot code: no "zero coding", "non-technical", "can't read code". He has a coding background.
- Never use an em dash (—). Use a period, comma or nothing.
- Never promise a DM, a link, a file or "I'll send you". Never ask them to DM.
- Never hide, mock, or argue with hostile comments. Disarm with humour or ask what they mean.
- Never invent facts about Marcus (his setup, numbers, revenue, products). If a good reply would need one you do not
  have, write the safest honest version and set needs_marcus=true with a one-line note.
- Never give medical/legal/financial advice. Never mention competitors negatively by name beyond light banter.
- Do not start with the commenter's @handle; the bot adds it when needed.
- Always reply in English, whatever language the comment is in (Marcus, 2026-10-11). When the comment is not in
  English, put a plain English translation of it in "translation" so Marcus can read it; otherwise leave it "".

Return ONLY JSON: {"reply": "...", "needs_marcus": false, "note": "", "translation": ""}"""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)


def _thread_text(thread, target_id):
    lines = []
    for c in sorted(thread, key=lambda c: c["timestamp"]):
        u = (c.get("from") or {}).get("username", "?")
        mark = ">> " if c["id"] == target_id else "   "
        lines.append(f"{mark}@{u}: {(c.get('text') or '').strip()}")
    return "\n".join(lines)


def _parse(txt):
    """Tolerant: clean JSON, then JSON inside prose, then a pattern match on the reply field, then bare text."""
    txt = txt.strip()
    for cand in (txt, (re.search(r"\{.*\}", txt, re.S) or re.match(r"$", "")).group(0)):
        if not cand:
            continue
        try:
            out = json.loads(cand)
            if isinstance(out, dict) and out.get("reply"):
                return out
        except ValueError:
            pass
    m = re.search(r'"reply"\s*:\s*"(.*?)"\s*,\s*"needs_marcus"\s*:\s*(true|false)', txt, re.S)
    if m:
        return {"reply": m.group(1).replace('\\"', '"'), "needs_marcus": m.group(2) == "true", "note": "parsed loosely"}
    if "{" not in txt and 0 < len(txt) <= 300:
        return {"reply": txt, "needs_marcus": True, "note": "model answered in prose"}
    raise ValueError("unparseable: " + txt[:120].replace("\n", " "))


_WORD = re.compile(r"[A-Za-z0-9']+")
_FIRST_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF❤️🔥]")


def is_short(text):
    """A one- or two-word reaction with no question: Marcus answers these with a word or an emoji, not a sentence."""
    text = text or ""
    return len(_WORD.findall(text)) <= 2 and "?" not in text


def _mirror_emoji(text):
    m = _FIRST_EMOJI.search(text or "")
    return m.group(0) if m and m.group(0) in "😂🤣🔥🙌❤️😍🥹💪👏💯🙏🫡✨" else "🙌"


def draft(cm, thread, caption, label):
    """Returns dict(reply, needs_marcus, note). Retries once, then falls back to a safe emoji (always flagged)."""
    short = is_short(cm.get("text"))
    user_msg = (f"REEL: {label}\nCAPTION: {caption[:300]}\n\nTHREAD (reply to the line marked >>):\n"
                f"{_thread_text(thread, cm['id'])}\n\nWrite Marcus's reply.")
    if short:
        user_msg += ("\nThe comment is a one-word or emoji reaction, so the reply is ONE emoji or at most two words "
                     "plus an emoji. No sentence, no question.")
    system = (SYSTEM.replace("{examples}", "\n".join(f"- {e}" for e in _examples[:150]))
                    .replace("{faq}", "\n".join(f"- {f['topic']}: {f['say']}" for f in _faq) or "- (none yet)"))
    last = None
    for attempt in range(2):
        try:
            # thinking is always on for Opus/Sonnet 5.5 and counts against max_tokens, so leave headroom;
            # effort is set explicitly because Opus 5.5 defaults to medium and Sonnet 5.5 to high
            r = _client().messages.create(model=config.DRAFT_MODEL, max_tokens=4096, system=system,
                                          output_config={"effort": config.DRAFT_EFFORT},
                                          messages=[{"role": "user", "content": user_msg}])
            txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            out = _parse(txt)
            reply = (out.get("reply") or "").strip().replace("—", ",")
            if not reply or len(reply) > 400:
                raise ValueError("empty/long reply: " + txt[:120])
            if short and (len(_WORD.findall(reply)) > 3 or "?" in reply):
                if attempt == 0:
                    raise ValueError("sentence for a one-word comment: " + reply[:80])
                reply = _mirror_emoji(cm.get("text"))   # second miss: mirror their emoji, never a sentence
            return {"reply": reply, "needs_marcus": bool(out.get("needs_marcus")), "note": str(out.get("note") or "")[:200],
                    "translation": str(out.get("translation") or "")[:300]}
        except Exception as e:
            last = e
            print(f"drafter attempt {attempt+1} failed for {cm.get('id')}: {type(e).__name__}: {str(e)[:200]}", flush=True)
    return {"reply": "🙌", "needs_marcus": True, "note": f"drafter failed twice ({type(last).__name__}), fallback emoji",
            "translation": ""}
