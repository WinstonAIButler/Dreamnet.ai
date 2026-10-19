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
- **Lifelike character library.** Seven starter characters (mafia heir, Bratva
  enforcer, vampire lord, bodyguard, billionaire rival, assassin, defense
  attorney). Supernatural characters are welcome, but everyone is written and
  described as lifelike, never cartoon or anime. Create your own with traits,
  backstory, look and an opening line.
- **Detailed character profiles.** Beyond the basics, each character has a
  profile in six sections: look (build, face, eyes, hair, skin, scars, tattoos,
  piercings, style, scent, voice), personality (temperament, desires, fears,
  flaws, habits, turn-ons, limits), life (job or business, wealth, house or
  apartment, car, origin, family, allies, enemies, skills, routine), past,
  supernatural (optional) and their relationship with you. Fill any field by
  hand, or press ✨ to have the AI write it; the AI box at the top can fill
  only the empty fields or the whole character from a one-line idea. Every
  field has a lock: public details are things you know or can see, while
  secret ones shape how the character acts but are only hinted at until the
  story earns the reveal.
- **Storylines.** Run as many separate roleplay storylines as you like. Each
  has its own title, premise, scenes (with any characters), clock and Day
  count, and the characters' memories stay inside it, so nothing leaks from one
  story into another. Mark a memory "every storyline" to share it. A new scene
  in a storyline picks up at that storyline's current time.
- **Roleplay scenes.** Chat in character with streaming replies, regenerate,
  rewind, start a new scene. Start a scene from a novel's cast and the character
  knows the story bible; any reply can be added to the latest chapter.
- **Long-term memory.** As a scene grows past the model's context window, older
  messages are folded into a "scene so far" summary, and lasting facts (names,
  promises, secrets, turning points) are saved to the character's memory, which
  every later scene with them reads. Both are visible and editable from the
  **Memory** button in a scene.
- **Exact memory.** A memory is only saved when the model can quote, word for
  word, the line in the scene that proves it; anything it can't quote is thrown
  away. Each memory shows that quote and when it was said. Rewinding a scene
  forgets what the rewound messages taught. Before every reply, the earlier
  lines that match what you just wrote (from this scene and the character's
  other scenes) are put back in front of the model verbatim.
- **Time marks and an in-story clock.** Every message shows when you wrote it,
  and every reply ends with the in-story time, like *[Current Time: 02:45 AM | Day 3]*.
  The scene header also shows the date, time of day and moon phase. The model
  picks how long each exchange took from the action-time table (dialogue,
  driving, blood feeding, rituals, combat, potions, spellcasting, intimacy,
  meals, sleep) and the app does the clock math, so time stays consistent
  through regenerates and rewinds. Edit the table and the pace (0.5x slow,
  1.0x real, 1.5x fast) on the **Time rules** page, and add rules for just one
  scene from its clock panel. During play, put [Time: 45 min] or
  [Time: 03:15 AM] in your message to set exactly how long the reply covers, or
  click any [Current Time] line to correct it; later messages move with it. Use the clock button in a
  scene to set the time, skip ahead or label a moment ("The next morning, his
  penthouse").
- **Read aloud.** Each character has a voice: pick a country (United States,
  United Kingdom, Australia, Canada, Ireland, New Zealand, South Africa, India,
  Singapore, the Philippines, Nigeria, Kenya, Tanzania, Hong Kong), a male or
  female voice from that country, a tone (deep, low, natural, bright) and a
  speed. Press **Listen** on any reply, or turn on auto-read with the 🔊 button.
  Choose whether to read everything, spoken words and thoughts, or spoken words
  only. All voices are English. They use Microsoft Edge's online Read Aloud
  voices through `edge-tts`, so they need internet; offline, the English voices
  installed on your computer read instead.
- **Styled roleplay text.** Spoken "words", *actions* and `thoughts` each have
  their own font and colour, and the characters are told to write that way.
- **Lifelike portraits.** One click turns a character card into a photorealistic
  portrait, using the Z-Image-Turbo workflow from Intel AI Playground on your
  own ComfyUI.
- **Scene pictures.** Press **🖼 Picture** under any message to turn that
  moment into a photorealistic picture. What that message says happens is the
  picture's action. Everything else is there to keep it accurate: the look,
  profile and remembered details of every character in it (the scene's
  character plus anyone from your library named in the moment), the storyline,
  the scene so far, the last few messages and the in-story time of day. The
  picture stays with the message. Make a new one, edit the prompt, or remove
  it. Uses the same ComfyUI as portraits, in landscape, and keeps it tasteful.
- **Export** a novel as one Markdown manuscript.
- **Installable** as an app on your phone or desktop (Add to Home Screen).

Hard lines that always apply: every character is an adult aged 18 or over, nothing may
depict or imply minors, and messages about self-harm get a crisis-line reply
instead of roleplay (see `app/safety.py`).

## Run it on your computer

Dreamnet runs on your own computer; nothing is hosted online. You need
Python 3.10+ ([python.org](https://www.python.org/downloads/)) and a
Featherless API key.

- **Windows:** double-click `start-windows.bat`.
- **Mac:** double-click `start-mac.command`.
- **Linux:** run `./start-mac-linux.sh`.

The first run installs what it needs and opens `.env`: add your
`FEATHERLESS_API_KEY` and an `ADMIN_PASSWORD`, save, and run the launcher
again. It opens http://localhost:8080 in your browser. To install it like an
app, use your browser's "Install app" option on that page.

Or by hand:

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

### Lifelike portraits (optional)

Portraits use the Z-Image-Turbo workflow from AI Playground's "Pro 2 Image"
preset, its most photorealistic image preset, sent to a ComfyUI server.

1. Get a ComfyUI with Z-Image-Turbo. The easy way is
   [AI Playground](https://github.com/intel/AI-Playground): install it, open
   the **Pro 2 Image** preset once so it downloads the models, and note the
   address of its ComfyUI (a port between 49000 and 49999). Any other ComfyUI
   works too with the three files from `Comfy-Org/z_image_turbo` (diffusion
   model, `qwen_3_4b` text encoder, `ae` VAE) in its models folders.
2. In `.env`, set `COMFYUI_URL` to that address. With AI Playground's ComfyUI,
   also set the model names the way it stores them:

   ```
   ZIMAGE_UNET=Comfy-Org---z_image_turbo\split_files\diffusion_models\z_image_turbo_bf16.safetensors
   ZIMAGE_CLIP=Comfy-Org---z_image_turbo\split_files\text_encoders\qwen_3_4b.safetensors
   ZIMAGE_VAE=Comfy-Org---z_image_turbo\split_files\vae\ae.safetensors
   ```
3. Open a character, click **Generate lifelike portrait**. The chat model writes
   a photo prompt from the card (you can edit it and regenerate), and the image
   becomes the character's picture. Portraits are saved in `portraits/`.

Portraits are always head-and-shoulders, clothed and of an adult.

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
| Chat history window (20 turns, 12k chars) and crisis phrases | JOSIE-Bot (MIT, see `docs/LICENSE-JOSIE-Bot.md`) |
| Installable app shell and service worker | ANEEMA app package |
| Long-term memory: capture, compress, re-inject | the claude-mem pattern, reimplemented in `app/studio.py` |
| Portrait workflow and photo-prompt instructions | Intel AI Playground `z-image.json` preset (MIT, see `docs/LICENSE-AI-Playground.md`) |
| Web app | `app/static/dreamnet/` (no build step) |

The studio calls the proxy's own streaming handler in-process, so every request
goes through the same connection-slot queue and retry logic as the
OpenAI-compatible `/v1/chat/completions` endpoint, which still works for other
tools.
