# @imabgold comment bot

Always-on worker on Railway. Every 10 minutes it reads new comments on the 3 newest reels + the 4 flagships, and
every hour (`SWEEP_MINUTES`) it sweeps every other reel on the account. It auto-replies to emoji/one-word reactions
and sends everything else to Marcus on Telegram with a Sonnet draft and Approve / Edit / Skip buttons. No scheduled
digest (`DIGEST_HOUR=-1`); `/status` and `/digest` on demand.

Files: `bot.py` loop · `classify.py` tiers · `draft.py` Sonnet · `ig.py` Graph API · `tg.py` Telegram · `state.py` sqlite.

## Deploy (first time, ~15 min, all by Marcus)

1. **Telegram bot**: message @BotFather → `/newbot` → name it (e.g. `imabgold_replies_bot`) → copy the token.
   Open your new bot, press Start, send `hi`.
2. **Chat id**: `TELEGRAM_BOT_TOKEN=<token> python3 -m bot.whoami` (run from `src/`) → prints `TELEGRAM_CHAT_ID=...`.
3. **Anthropic key**: console.anthropic.com → API keys → create one for "imabgold-bot".
4. **Railway**: from `src/bot/`
   ```bash
   railway login
   railway init          # new project, name it imabgold-bot
   railway up            # builds the Dockerfile and deploys
   ```
   Then in the Railway dashboard → the service:
   - **Variables**: paste every key from `.env.example` (IG token + user id from the project `.env`).
   - **Volume**: add one, mount path `/data` (keeps the sqlite db across deploys).
   - Redeploy once after adding variables.
5. Telegram should say "🤖 bot online". Send `/status`.

Updates later: edit the code, then from `src/bot`: `python3 build_history.py && railway up --service imabgold-bot --no-gitignore`.
`--no-gitignore` matters: `history.json` (other people's comments, built from the comment dumps) is gitignored so it never reaches the public repo, but it must ship in the image; `.dockerignore` governs the upload.

## Rehearsal before deploy (no posting, no Telegram)
```bash
cd src && ENV_FILE=../.env ANTHROPIC_API_KEY=... python3 -m bot.dry_run 2 5
```
Shows how the last 2 days of real comments would have been tiered and drafts 5 flagged ones.

## Guardrails (what the bot will never do)
- Graph API only. Never hides, deletes, likes, follows, DMs, or touches a browser session.
- Posts only (a) the auto tier when `AUTO_POST=1`, (b) what Marcus approved or typed on Telegram.
- Re-fetches the thread right before every post; if Marcus already replied in the app, it stands down.
- One reply per comment, ever. First sight of a reel records existing comments and acts on none of them.
- Every decision is a row in `/data/bot.db` with a status.

## Telegram
`/status` `/pause` `/resume` `/sweep` `/help`. Tap Edit on a card, then just type your text as the next message to post that instead of the draft (replying directly to the card works too).
`/pause` stops reading comments and auto-posting; your own taps and typed replies still post while paused.
`AUTO_POST` defaults to `0` (shadow) if the variable is missing, so a misconfigured deploy never auto-posts.
