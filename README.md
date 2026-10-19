# Dreamnet

A private, single-user studio for writing dark romance novels, with interactive
character roleplay in the style of ourdream.ai. It runs on your own machine or
server and talks to open-weight roleplay models through [Featherless.ai](https://featherless.ai).

## What it does

- **Novels with a story bible.** Each novel has a premise, tropes, setting, point
  of view, tone, heat level, style notes and a cast. The AI sees all of it every
  time it writes, so voice and continuity hold.
- **Chapter editor with an AI co-author.** Continue, write a scene, dialogue,
  description, rewrite a selection, brainstorm or outline. Output streams into a
  side panel and only lands in your chapter when you append, insert or replace.
  Chapters autosave, can be reordered, and can be summarized so later chapters
  remember what happened.
- **Character library.** Six starter characters (mafia heir, vampire lord,
  bodyguard, billionaire rival, assassin, and Josie from JOSIE-Bot). Create your
  own with traits, backstory, look and an opening line.
- **Roleplay scenes.** Chat in character with streaming replies, regenerate,
  rewind, start a new scene. Start a scene from a novel's cast and the character
  knows the story bible; any reply can be added to the latest chapter.
- **Export** a novel as one Markdown manuscript.
- **Installable** as an app on your phone or desktop (Add to Home Screen).

Hard lines that always apply: every character must be 18 or older, nothing may
depict or imply minors, and messages about self-harm get a crisis-line reply
instead of roleplay (see `app/safety.py`).

## Run it

Needs Python 3.10+ and a Featherless API key.

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: FEATHERLESS_API_KEY, ADMIN_PASSWORD, MAX_CONCURRENT_CONNECTIONS for your plan
python -m app.main
```

Open http://localhost:8080 and sign in as `admin` with your `ADMIN_PASSWORD`.
`ADMIN_PASSWORD` is only read when the database is first created; change it
later from the dashboard at `/dashboard`.

Your writing lives in the SQLite file named by `DATABASE_PATH` (default
`proxy.db`). Back that file up.

### Models

Two Featherless models are added on first start: Stheno 8B (roleplay, the
default) and NemoMix Unleashed 12B (longer prose). Add any other Featherless
model in **admin ▾ → Models & usage**, then pick it per novel in the story bible.
Set `DREAMNET_DEFAULT_MODEL` to change the default for chats.

### Keep it private

It is built for one person. If you host it anywhere other than your own
computer, put it behind HTTPS, set a strong `ADMIN_PASSWORD`, and don't create
other accounts.

## How it's built

| Part | Where it came from |
| --- | --- |
| Backend, queue, retries, usage tracking, admin panel | `featherless-proxy` (docs in `docs/featherless-proxy.md`) |
| Studio API: characters, novels, chapters, chats, writing tools | `app/studio.py` (new) |
| Safety checks and crisis reply | `app/safety.py`, crisis phrases from JOSIE-Bot |
| Chat history window (20 turns, 12k chars) and Josie | JOSIE-Bot (MIT, see `docs/LICENSE-JOSIE-Bot.md`) |
| Installable app shell and service worker | ANEEMA app package |
| Web app | `app/static/dreamnet/` (no build step) |

The studio calls the proxy's own streaming handler in-process, so every request
goes through the same connection-slot queue and retry logic as the
OpenAI-compatible `/v1/chat/completions` endpoint, which still works for other
tools.
