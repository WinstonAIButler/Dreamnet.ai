"""
Dreamnet - private writing studio
A single-user app for writing dark romance novels: a character library,
in-character roleplay chats, novels with a story bible and chapters, and AI
writing tools. Every model call goes through the proxy's own streaming
handler, so it shares the proxy's queue, retries and usage logging.
"""
import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import app.api_routes as api_routes
import app.portraits as portraits
from app.auth import get_session_from_request
from app.database import (
    DB_PATH, create_api_key, create_model, get_api_key_by_key, get_model_by_id, list_models,
)
from app.safety import CRISIS_RESPONSE, SAFETY_RULES, check_character, is_crisis, mentions_minor

router = APIRouter(prefix="/app/api")

DEFAULT_MODEL = os.getenv("DREAMNET_DEFAULT_MODEL", "Sao10K/L3-8B-Stheno-v3.2")
WEB_KEY_NAME = "dreamnet-web"

# Chat context window, the same limits JOSIE uses on device.
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 12_000
# Long-term memory: once this many older messages have scrolled out of the
# context window, they are folded into the scene summary and new lasting
# facts are saved to the character's memories (the claude-mem pattern:
# capture, compress, re-inject).
COMPACT_EVERY = 8
MAX_MEMORIES_IN_PROMPT = 25
# Exact recall: verbatim earlier lines (this scene and the character's other
# scenes) that match the new message are put back in front of the model, so
# details are quoted rather than paraphrased from a summary.
RECALL_LINES = 6
RECALL_CHARS = 3_000
# How much of the current chapter the writing tools see.
CHAPTER_CONTEXT_CHARS = 8_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age >= 18),
    gender TEXT NOT NULL DEFAULT 'female',
    tagline TEXT NOT NULL DEFAULT '',
    appearance TEXT NOT NULL DEFAULT '',
    persona TEXT NOT NULL,
    greeting TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',  -- portrait URL, empty = gradient with initial
    portrait_prompt TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#be123c',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS novels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    premise TEXT NOT NULL DEFAULT '',
    tropes TEXT NOT NULL DEFAULT '',
    setting TEXT NOT NULL DEFAULT '',
    pov TEXT NOT NULL DEFAULT 'first person, present tense',
    tone TEXT NOT NULL DEFAULT 'dark, obsessive, slow-burn',
    heat TEXT NOT NULL DEFAULT 'explicit',
    style_notes TEXT NOT NULL DEFAULT '',
    model_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS novel_cast (
    novel_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (novel_id, character_id)
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_novel ON chapters(novel_id, position);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    novel_id INTEGER,                 -- set = the roleplay knows that story bible
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',           -- rolling "story so far" of older turns
    summarized_upto INTEGER NOT NULL DEFAULT 0, -- last message id folded into summary
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Lasting facts a character remembers across every scene.
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',        -- auto | user
    conversation_id INTEGER,                    -- auto: the scene it came from
    message_id INTEGER,                         -- auto: the message that proves it
    quote TEXT NOT NULL DEFAULT '',             -- auto: exact words from that message
    said_at REAL,                               -- when that message was sent
    story_time TEXT NOT NULL DEFAULT '',        -- story-time mark in force then
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_char ON memories(character_id, id);

-- Key/value settings for the studio (time rules).
CREATE TABLE IF NOT EXISTS studio_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,               -- user | assistant | mark (story-time mark)
    content TEXT NOT NULL,
    story_at TEXT NOT NULL DEFAULT '', -- in-universe clock at this message (ISO, no zone)
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
"""

# Roleplay and prose models on Featherless (JOSIE's Stheno plus a 12B).
# Prices are only used for the usage stats in the admin panel.
SEED_MODELS = [
    ("Sao10K/L3-8B-Stheno-v3.2", "Stheno 8B (roleplay)", 1, 0.20, 0.05, 0.40),
    ("MarinaraSpaghetti/NemoMix-Unleashed-12B", "NemoMix Unleashed 12B (prose)", 1, 0.30, 0.08, 0.60),
]

SEED_CHARACTERS = [
    dict(name="Dante Moretti", age=34, gender="male", color="#991b1b",
         tagline="Heir to a crime family who never asks twice",
         appearance="Tall, dark hair pushed back, a scar through one eyebrow, tailored black suits, a signet ring.",
         persona="Dante is the cold, controlled heir of the Moretti family. Possessive, calculating and quietly "
                 "dangerous, he speaks softly and rarely raises his voice because he never needs to. He keeps "
                 "his promises and his grudges, and his rare tenderness is reserved for the one person he has "
                 "decided is his.",
         greeting="*doesn't look up from the ledger* You walked into the wrong room, cara. Close the door. Now tell me why I shouldn't keep you here.",
         tags="mafia,possessive,morally grey"),
    dict(name="Nikolai Volkov", age=37, gender="male", color="#475569",
         tagline="Bratva enforcer who was sent to collect your debt",
         appearance="Six foot four, close-cropped blond hair, pale grey eyes, tattooed hands, a worn leather coat.",
         persona="Nikolai is the quiet enforcer of a Russian crime family in Chicago. Patient, observant and "
                 "unsettlingly calm, he speaks little and means every word. He grew up with nothing, trusts no one, "
                 "and is thrown by how badly he wants to protect the person he was sent to frighten.",
         greeting="*sets your unpaid ledger on the kitchen table and sits down without being asked* Relax. If I wanted to hurt you, you'd already know. We need to talk about your brother.",
         tags="bratva,stalker,protective"),
    dict(name="Lucien Vale", age=312, gender="male", color="#4c1d95",
         tagline="Vampire lord who has waited centuries for you",
         appearance="Tall and lean, porcelain skin with faint blue veins at the temples, sharp cheekbones, long black "
                    "hair tied back, dark eyes that catch the light red, a fitted black wool coat over an open shirt.",
         persona="Lucien is an ancient vampire who passes for a reclusive collector in modern-day New Orleans. "
                 "Elegant, melancholy and predatory, he speaks with old-world formality, is fascinated by the person "
                 "in front of him, and fights his hunger as much as he indulges it.",
         greeting="*steps out of the shadow of the bookshelves, cold air following him* Three hundred years, and you still arrive late. Come closer. I don't bite... unless asked.",
         tags="vampire,gothic,obsessive"),
    dict(name="Rhys Callahan", age=36, gender="male", color="#1e3a8a",
         tagline="Ex-mercenary bodyguard who hates this job",
         appearance="Broad, scarred knuckles, buzzed hair, grey eyes, black tactical jacket, a gun he never hides.",
         persona="Rhys is a gruff former mercenary hired to protect someone he finds infuriating. Blunt, sarcastic and "
                 "fiercely protective, he hides a violent past and a soft spot he would rather die than admit.",
         greeting="*checks the window, then you* Rule one: you go nowhere without me. Rule two: stop looking at me like that.",
         tags="bodyguard,enemies to lovers,protective"),
    dict(name="Adrian Kross", age=38, gender="male", color="#0f766e",
         tagline="Billionaire rival who bought your company to own you",
         appearance="Silver-touched dark hair, clean-shaven, ice-blue eyes, immaculate charcoal suits.",
         persona="Adrian is a ruthless tech billionaire who wins everything he wants. Arrogant, brilliant and "
                 "manipulative, he enjoys verbal duels and slowly reveals an obsession that started years ago.",
         greeting="*signs the last page and slides it across the desk* Congratulations. You work for me now. Sit down, we have a lot to discuss.",
         tags="billionaire,rivals,power play"),
    dict(name="Mara Voss", age=29, gender="female", color="#be123c",
         tagline="Assassin sent to kill you, who didn't",
         appearance="Black hair in a sharp braid, dark eyes, a knife scar on her collarbone, a fitted leather jacket.",
         persona="Mara is a cold, elite assassin who broke contract for the first time when she met her target. "
                 "Guarded, dry-humoured and deadly, she trusts no one and is furious that she cares.",
         greeting="*the blade rests against your throat, then lowers* You should be dead. Give me one reason I didn't finish the job.",
         tags="assassin,dangerous,slow-burn"),
    dict(name="Sienna Hart", age=28, gender="female", color="#b45309",
         tagline="Defense attorney who knows exactly what you did",
         appearance="Dark auburn hair in a low knot, green eyes, sharp tailored suit, red-soled heels, no jewelry.",
         persona="Sienna is a brilliant criminal defense attorney who represents dangerous men and wins. Cool, "
                 "precise and impossible to intimidate, she enjoys control and verbal chess, keeps her own past locked "
                 "away, and is drawn to people who don't bore her.",
         greeting="*clicks her pen and studies you across the interview table* You lied to the police twice. Lie to me once and I walk. Start from the beginning.",
         tags="lawyer,power play,slow-burn"),
]

# Characters can be human or supernatural, but always look and feel lifelike.
LIFELIKE_RULES = (
    "You are lifelike: describe yourself and the world in realistic, photographic physical detail (skin, light, "
    "texture, scent, body language), never cartoonish or anime-like. Supernatural traits are allowed but stay "
    "grounded and physical, as if they were real. Talk the way a real person talks, with natural rhythm and real "
    "emotions, and keep consistent memories, moods and motives."
)

ASSIST_MODES = {
    "continue": "Continue the chapter directly from where the text ends. Do not repeat what is already written.",
    "scene": "Write the scene described in the instruction, in a way that fits after the current text.",
    "dialogue": "Write a dialogue-heavy passage between the characters for the moment described in the instruction.",
    "describe": "Write a rich, sensory description of what the instruction names: a place, a person, a moment.",
    "rewrite": "Rewrite the selected passage following the instruction. Return only the rewritten passage.",
    "brainstorm": "Brainstorm ideas for the instruction as a numbered list: plot twists, scene ideas, lines of dialogue. Do not write prose.",
    "outline": "Propose an outline of the next chapters as a numbered list of beats, building tension toward the climax.",
}
ASSIST_LENGTH = {"short": 350, "medium": 750, "long": 1400}

# In-universe clock. The model only estimates how long each exchange takes and
# reports it in a [Time passed: ...] tag; the app does the clock arithmetic,
# strips the tag, and stamps every message with the resulting time, so the clock
# stays consistent through regenerates and rewinds.
DEFAULT_MINUTES = 3
MAX_SKIP_MINUTES = 7 * 24 * 60
PACES = (0.5, 1.0, 1.5)  # 0.5x slow, 1.0x real, 1.5x fast: multiplies every time cost

# Action-time formulas, editable on the Time rules page. Minutes; under 1 means seconds.
DEFAULT_TIME_ACTIONS = [
    {"label": "Standard dialogue or minor action", "min": 1, "max": 5, "per": "message", "context": "Talking, reacting, small movements."},
    {"label": "Messaging / texting", "min": 1, "max": 3, "per": "exchange", "context": "Stalking, sending threatening or teasing texts."},
    {"label": "Short-distance driving", "min": 1.5, "max": 1.5, "per": "mile", "context": "Navigating city streets, pursuing a target."},
    {"label": "Long-distance driving", "min": 1, "max": 1, "per": "mile", "context": "Fleeing to an isolated cabin or safehouse."},
    {"label": "Blood feeding (quick)", "min": 3, "max": 5, "per": "", "context": "A frantic, rough bite in an alleyway or corridor."},
    {"label": "Blood feeding (sensual)", "min": 15, "max": 30, "per": "", "context": "Slow, intimate or predatory feeding mixed with skin-to-skin contact."},
    {"label": "Sacrifices / dark rituals", "min": 60, "max": 180, "per": "", "context": "Chanting, preparation and execution of a blood rite."},
    {"label": "Combat / physical altercations", "min": 0.17, "max": 0.5, "per": "round", "context": "2 to 5 minutes total. Brutal, fast fights, restraints, subduing a target."},
    {"label": "Crafting potions or poisons", "min": 30, "max": 120, "per": "", "context": "Brewing toxins, grinding herbs, preparing aphrodisiacs."},
    {"label": "Spellcasting (quick)", "min": 0.17, "max": 0.5, "per": "", "context": "Hexes, mind control, shadow manipulation in the moment."},
    {"label": "Intimacy (foreplay / mild)", "min": 15, "max": 30, "per": "", "context": "Heavy petting, teasing, dark psychological tension building."},
    {"label": "Intimacy (full sexual acts)", "min": 30, "max": 90, "per": "", "context": "The main event, multiple rounds, prolonged intense encounters."},
    {"label": "Meal (quick snack or coffee)", "min": 5, "max": 10, "per": "", "context": "Grabbing coffee, eating on the move."},
    {"label": "Meal (sit-down dinner)", "min": 45, "max": 90, "per": "", "context": "Restaurant dining, cooking a full meal."},
    {"label": "Power nap", "min": 20, "max": 30, "per": "", "context": "A quick reset."},
    {"label": "Sleep (restless / forced)", "min": 240, "max": 360, "per": "", "context": "Passing out from exhaustion, trauma or being drugged."},
    {"label": "Sleep (full overnight)", "min": 360, "max": 480, "per": "", "context": "Fast-forwarding to the next evening or night."},
]


def _span(minutes: float) -> str:
    """3 -> "3 minutes", 0.17 -> "10 seconds", 90 -> "1 hour 30 minutes"."""
    if minutes < 1:
        return f"{round(minutes * 60)} seconds"
    h, m = divmod(round(minutes), 60)
    return " ".join(p for p in (f"{h} hour{'s' if h != 1 else ''}" if h else "",
                                f"{m} minute{'s' if m != 1 else ''}" if m else "") if p) or "0 minutes"


def _time_rules(actions: list[dict]) -> str:
    """The time protocol for the system prompt, built from the action-time table."""
    table = "\n".join(
        f"   - {a['label']}: " + (_span(a["min"]) if a["min"] == a["max"] else f"{_span(a['min'])} to {_span(a['max'])}")
        + (f" per {a['per']}" if a.get("per") else "") + (f". {a['context']}" if a.get("context") else "")
        for a in actions)
    return f"""[TIME MANAGEMENT PROTOCOL - CRITICAL]
1. Track the in-universe time and Day meticulously. The current time, Day, time of day and moon phase are given
   above, and the app prints the time at the end of every reply as *[Current Time: 02:45 AM | Day 3]*, so never
   write that line yourself.
2. Before writing, evaluate the actions taken by both the user and you, and work out how much in-universe time they
   take together, using these action-time formulas:
{table}
3. Dark romance pacing: do not rush intimacy, combat or feeding scenes. When an act is prolonged, let it run across
   several replies, each covering only the time it actually describes, so the clock reflects time passing
   naturally rather than jumping to the end of the act.
4. Keep time consistent. If a character is knocked out or goes to sleep at 03:00 AM for 5 hours, the next reply
   starts at exactly 08:00 AM. Never contradict the current time, the Day, daylight or darkness, or the moon.
5. If an action skips hours (long drives, deep sleep, complex rituals), write the narrative transition describing
   the passage of time before the time moves on.
End every reply with one final line stating only how much time passed, exactly like [Time passed: 4 minutes],
[Time passed: 40 seconds] or [Time passed: 5 hours]. The app adds it to the clock and prints the new time."""


async def _time_settings() -> dict:
    row = await _row("SELECT value FROM studio_settings WHERE key = 'time_rules'")
    data = json.loads(row["value"]) if row else {}
    return {"pace": data.get("pace", 1.0), "actions": data.get("actions") or DEFAULT_TIME_ACTIONS}


_TIME_TAG = re.compile(r"[*_]*\[\s*(?:time passed|elapsed time|current time)\s*:[^\]]*\][*_]*", re.IGNORECASE)
_PASSED = re.compile(r"\[\s*(?:time passed|elapsed time)\s*:([^\]]*)\]", re.IGNORECASE)
_UNITS = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d\b|hours?|hrs?|h\b|minutes?|mins?|m\b|seconds?|secs?|s\b)",
                    re.IGNORECASE)


async def init_studio_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(characters)")).fetchall()]
        if "portrait_prompt" not in cols:
            await db.execute("ALTER TABLE characters ADD COLUMN portrait_prompt TEXT NOT NULL DEFAULT ''")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(conversations)")).fetchall()]
        if "summary" not in cols:
            await db.execute("ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            await db.execute("ALTER TABLE conversations ADD COLUMN summarized_upto INTEGER NOT NULL DEFAULT 0")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(messages)")).fetchall()]
        if "story_at" not in cols:
            await db.execute("ALTER TABLE messages ADD COLUMN story_at TEXT NOT NULL DEFAULT ''")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(memories)")).fetchall()]
        for col, kind in (("conversation_id", "INTEGER"), ("message_id", "INTEGER"),
                          ("quote", "TEXT NOT NULL DEFAULT ''"), ("said_at", "REAL"),
                          ("story_time", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {kind}")
        await db.commit()
        n = (await (await db.execute("SELECT COUNT(*) FROM characters")).fetchone())[0]
        if n == 0:
            now = time.time()
            for c in SEED_CHARACTERS:
                await db.execute(
                    """INSERT INTO characters (name, age, gender, tagline, appearance, persona, greeting,
                       tags, avatar, color, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (c["name"], c["age"], c["gender"], c["tagline"], c["appearance"], c["persona"],
                     c["greeting"], c["tags"], c.get("avatar", ""), c["color"], now, now))
            await db.commit()
    if not await list_models(db_path):
        for m in SEED_MODELS:
            await create_model(db_path, *m)


# --- helpers ---

def _err(status: int, msg: str):
    return JSONResponse({"error": msg}, status_code=status)


async def _session(request: Request) -> dict:
    s = await get_session_from_request(DB_PATH, request)
    if not s:
        raise HTTPException(status_code=401, detail="Please sign in")
    return s


async def _rows(sql: str, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return [dict(r) for r in await (await db.execute(sql, args)).fetchall()]


async def _row(sql: str, args=()):
    rows = await _rows(sql, args)
    return rows[0] if rows else None


async def _exec(sql: str, args=()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid


async def _web_key(user_id: int) -> dict:
    """The API key the website uses to call the proxy on the user's behalf."""
    row = await _row("SELECT key FROM api_keys WHERE user_id = ? AND name = ? AND enabled = 1",
                     (user_id, WEB_KEY_NAME))
    key = row["key"] if row else None
    if not key:
        key = "fp_" + secrets.token_urlsafe(32)
        await create_api_key(DB_PATH, key, WEB_KEY_NAME, user_id, 1)
    return await get_api_key_by_key(DB_PATH, key)


async def _stream(request: Request, session: dict, model: str, messages: list[dict],
                  temperature: float, max_tokens: int, on_done=None):
    """Stream a completion through the proxy and call ``on_done(text)`` at the end."""
    key = await _web_key(session["user_id"])
    is_admin = api_routes._is_admin_key(key)
    if not is_admin and (key.get("user_credits") or 0) <= 0:
        return _err(402, "This account has no credits left. Top it up in the admin panel.")
    body = {"model": model, "messages": messages, "stream": True,
            "temperature": temperature, "top_p": 0.95, "max_tokens": max_tokens}
    cfg = api_routes._resolve_model(await get_model_by_id(DB_PATH, model))
    hit = await api_routes.cache.get(model, messages, temperature=temperature)
    cached_ratio = hit.get("_cached_ratio", 0.0) if hit else 0.0
    upstream = await api_routes._handle_streaming(
        request, body, model, messages, temperature, cfg, key, session["user_id"], is_admin,
        key.get("priority", 1), cached_ratio)

    async def relay():
        text, buf = "", ""
        async for chunk in upstream.body_iterator:
            raw = chunk if isinstance(chunk, bytes) else chunk.encode()
            buf += raw.decode("utf-8", errors="replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    for ch in data.get("choices", []):
                        text += (ch.get("delta") or {}).get("content") or ""
            yield raw
        if on_done and text.strip():
            await on_done(text.strip())

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _bible(novel: dict, cast: list[dict]) -> str:
    lines = [f"Novel: {novel['title']}"]
    for label, key in (("Premise", "premise"), ("Tropes", "tropes"), ("Setting", "setting"),
                       ("Point of view", "pov"), ("Tone", "tone"), ("Heat level", "heat"),
                       ("Style notes", "style_notes")):
        if novel.get(key):
            lines.append(f"{label}: {novel[key]}")
    if cast:
        lines.append("Cast:")
        for c in cast:
            role = f" ({c['role']})" if c.get("role") else ""
            lines.append(f"- {c['name']}, {c['age']}{role}: {c['persona']} Looks: {c['appearance']}")
    return "\n".join(lines)


async def _novel_with_cast(novel_id: int):
    novel = await _row("SELECT * FROM novels WHERE id = ?", (novel_id,))
    if not novel:
        return None, []
    cast = await _rows("""SELECT c.*, nc.role FROM novel_cast nc JOIN characters c ON c.id = nc.character_id
                          WHERE nc.novel_id = ? ORDER BY c.name""", (novel_id,))
    return novel, cast


def _history_window(history: list[dict]) -> list[dict]:
    """Newest turns that fit within MAX_HISTORY_TURNS and MAX_HISTORY_CHARS."""
    out, chars = [], 0
    for m in reversed(history[-MAX_HISTORY_TURNS * 2:]):
        chars += len(m["content"])
        if chars > MAX_HISTORY_CHARS and out:
            break
        out.append({"role": m["role"], "content": m["content"], "story_at": m.get("story_at", "")})
    return list(reversed(out))


def _chat_turns(window: list[dict], start: str = "") -> list[dict]:
    """Turn a history window into chat messages: story-time marks become a note on
    the user's side, and same-role neighbours are merged, since some chat templates
    (Mistral's) require user and assistant turns to alternate."""
    out = []
    for m in window:
        role, content = m["role"], m["content"]
        if role == "mark":
            role, content = "user", f"[Story time: {content}" + (
                f" | now {_clock_text(m['story_at'], start)}]" if m.get("story_at") else "]")
        elif role == "assistant" and m.get("story_at"):
            content += "\n" + _now_line(m["story_at"], start)
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


def _day(iso: str, start: str) -> int:
    """Day number of the scene: the day it starts on is Day 1."""
    if not iso or not start:
        return 1
    return max(1, (datetime.fromisoformat(iso).date() - datetime.fromisoformat(start).date()).days + 1)


def _clock_text(iso: str, start: str = "") -> str:
    """In-universe time: "Fri Oct 16, 11:40 PM", or "Day 2, Sat Oct 17, 02:45 AM" when the scene start is known."""
    if not iso:
        return ""
    d = datetime.fromisoformat(iso)
    return (f"Day {_day(iso, start)}, " if start else "") + d.strftime("%a %b %d, %I:%M %p")


def _now_line(iso: str, start: str) -> str:
    """The line the app prints after every reply: *[Current Time: 02:45 AM | Day 3]*."""
    return f"*[Current Time: {datetime.fromisoformat(iso).strftime('%I:%M %p')} | Day {_day(iso, start)}]*"


async def _scene_start(conv_id: int) -> str:
    """In-universe time the scene began (its first stamped message)."""
    first = await _row("SELECT story_at FROM messages WHERE conversation_id = ? AND story_at != '' "
                       "ORDER BY id LIMIT 1", (conv_id,))
    return first["story_at"] if first else ""


def _iso(d: datetime) -> str:
    return d.replace(microsecond=0).isoformat(timespec="seconds")


_PHASES = ("New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon", "Waning gibbous",
           "Last quarter", "Waning crescent")


def _moon(d: datetime) -> str:
    """Moon phase for a date, from the mean synodic month since the new moon of 6 Jan 2000."""
    age = ((d - datetime(2000, 1, 6, 18, 14)).total_seconds() / 86400) % 29.530588853
    return _PHASES[int(age / 29.530588853 * 8 + 0.5) % 8]


def _daypart(d: datetime) -> str:
    h = d.hour
    return "night" if h >= 21 or h < 5 else "dawn" if h < 7 else "day" if h < 18 else "dusk"


def _elapsed(reply: str, pace: float = 1.0) -> tuple[str, float]:
    """Strip the time tags from a reply; return the clean text and the minutes that passed, times the pace."""
    minutes = None
    tag = _PASSED.findall(reply)
    if tag:
        total = 0.0
        for n, unit in _UNITS.findall(tag[-1]):
            u = unit.lower()[0]
            total += float(n) * (1440 if u == "d" else 60 if u == "h" else 1 / 60 if u == "s" else 1)
        minutes = total if _UNITS.search(tag[-1]) else None
    clean = _TIME_TAG.sub("", reply).rstrip()
    if minutes is None:
        minutes = DEFAULT_MINUTES
    return clean or reply, max(0.0, min(minutes * pace, MAX_SKIP_MINUTES))


async def _scene_clock(cv: dict) -> datetime:
    """The scene's in-universe time: the stamp on its latest message, or when the scene started."""
    last = await _row("SELECT story_at FROM messages WHERE conversation_id = ? AND story_at != '' "
                      "ORDER BY id DESC LIMIT 1", (cv["id"],))
    if last:
        return datetime.fromisoformat(last["story_at"])
    return datetime.fromtimestamp(cv["created_at"]).replace(second=0, microsecond=0)


def _stamp(ts: float | None) -> str:
    """A time mark as people write it, in the computer's local time: Oct 18, 2026 9:04 PM."""
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    return d.strftime("%b %d, %Y ") + d.strftime("%I:%M %p").lstrip("0")


def _norm(text: str) -> str:
    """Normalize text for exact-quote checks: case, curly quotes, asterisks, spacing."""
    text = text.lower().translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                                                 "\u2014": "-", "\u2013": "-", "*": " ", "_": " "}))
    return re.sub(r"\s+", " ", text).strip(" .,!?\"'")


_STOP = set("""a an and are as at be been but by can could did do does for from had has have he her hers him his how i
if in into is it its just me my no not now of on or our out she so than that the their them then there they this
to too up us was we were what when where which who why will with would you your yours i'm i'll it's don't""".split())


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", text.lower()) if w not in _STOP}


async def _recall(c: dict, conv_id: int, older_ids_below: int, query: str, username: str) -> list[dict]:
    """Verbatim earlier messages that best match the new message: from this scene
    (before the context window) and from the character's other scenes."""
    words = _keywords(query)
    if not words:
        return []
    rows = await _rows(
        """SELECT m.id, m.conversation_id, m.role, m.content, m.story_at, m.created_at FROM messages m
           JOIN conversations cv ON cv.id = m.conversation_id
           WHERE cv.character_id = ? AND m.role IN ('user', 'assistant')
             AND (m.conversation_id != ? OR m.id < ?)""", (c["id"], conv_id, older_ids_below))
    scored = []
    for m in rows:
        hits = words & _keywords(m["content"])
        if len(hits) >= min(2, len(words)):
            scored.append((len(hits), m["id"], m))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    picked, chars = [], 0
    for _, _, m in scored[:RECALL_LINES]:
        text = m["content"] if len(m["content"]) <= 600 else m["content"][:600] + "..."
        chars += len(text)
        if chars > RECALL_CHARS and picked:
            break
        picked.append({**m, "content": text, "who": c["name"] if m["role"] == "assistant" else username})
    return sorted(picked, key=lambda m: m["id"])


def _character_fields(body: dict) -> dict:
    f = {k: str(body.get(k) or "").strip()[:4000] for k in
         ("name", "gender", "tagline", "appearance", "persona", "greeting", "tags", "color", "avatar")}
    f["age"] = body.get("age")
    return f


# --- account / models ---

@router.get("/me")
async def me(request: Request):
    s = await get_session_from_request(DB_PATH, request)
    if not s:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "username": s["username"], "is_admin": bool(s["is_admin"]),
                         "default_model": DEFAULT_MODEL, "portraits": portraits.enabled()})


@router.get("/models")
async def models(s: dict = Depends(_session)):
    return JSONResponse([{"model_id": m["model_id"], "display_name": m["display_name"]}
                         for m in await list_models(DB_PATH)])


# --- characters ---

@router.get("/characters")
async def characters(s: dict = Depends(_session)):
    return JSONResponse(await _rows("SELECT * FROM characters ORDER BY updated_at DESC"))


@router.get("/characters/{cid}")
async def character(cid: int, s: dict = Depends(_session)):
    c = await _row("SELECT * FROM characters WHERE id = ?", (cid,))
    return JSONResponse(c) if c else _err(404, "Character not found")


@router.post("/characters")
async def save_character(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    f = _character_fields(body)
    if not f["name"] or not f["persona"]:
        return _err(400, "Give the character a name and a personality.")
    problem = check_character(f)
    if problem:
        return _err(400, problem)
    greeting = f["greeting"] or f"*looks at you* So. You're finally here."
    now = time.time()
    cid = body.get("id")
    if cid:
        await _exec("""UPDATE characters SET name=?, age=?, gender=?, tagline=?, appearance=?, persona=?,
                       greeting=?, tags=?, color=?, avatar=?, updated_at=? WHERE id=?""",
                    (f["name"][:60], int(f["age"]), f["gender"] or "female", f["tagline"][:160], f["appearance"],
                     f["persona"], greeting, f["tags"][:200], f["color"] or "#be123c", f["avatar"], now, int(cid)))
        return JSONResponse({"id": int(cid)})
    cid = await _exec("""INSERT INTO characters (name, age, gender, tagline, appearance, persona, greeting,
                         tags, avatar, color, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (f["name"][:60], int(f["age"]), f["gender"] or "female", f["tagline"][:160], f["appearance"],
                       f["persona"], greeting, f["tags"][:200], f["avatar"], f["color"] or "#be123c", now, now))
    return JSONResponse({"id": cid})


@router.post("/characters/{cid}/portrait")
async def make_portrait(cid: int, request: Request, s: dict = Depends(_session)):
    """Generate a lifelike portrait with ComfyUI and set it as the character's picture.
    With no prompt in the body, the chat model writes one from the character card."""
    c = await _row("SELECT * FROM characters WHERE id = ?", (cid,))
    if not c:
        return _err(404, "Character not found")
    if not portraits.enabled():
        return _err(503, "Portraits need a ComfyUI server. Set COMFYUI_URL in .env (see README).")
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()[:4000]
    if not prompt:
        prompt = await _portrait_prompt(request, s, c)
    if mentions_minor(prompt, strict=True):
        return _err(400, "Portraits must show an adult.")
    try:
        png = await portraits.generate(api_routes.http_client, prompt)
    except portraits.PortraitError as e:
        return _err(502, str(e))
    url = portraits.save(cid, png)
    await _exec("UPDATE characters SET avatar = ?, portrait_prompt = ?, updated_at = ? WHERE id = ?",
                (url, prompt, time.time(), cid))
    return JSONResponse({"avatar": url, "prompt": prompt})


async def _portrait_prompt(request: Request, s: dict, c: dict) -> str:
    """Ask the chat model to expand the character card into a Z-Image prompt."""
    card = (f"Name: {c['name']}\nAge: {c['age']}\nGender: {c['gender']}\nTagline: {c['tagline']}\n"
            f"Appearance: {c['appearance']}\nPersonality: {c['persona']}")
    messages = [{"role": "system", "content": portraits.PROMPT_INSTRUCTIONS},
                {"role": "user", "content": card}]
    try:
        text = (await _complete(s["user_id"], messages, 0.7, 300)).strip().strip('"')
        if len(text) > 40:
            return text
    except Exception:
        pass
    return portraits.template_prompt(c)


async def _complete(user_id: int, messages: list[dict], temperature: float, max_tokens: int,
                    model: str | None = None) -> str:
    """One non-streaming completion through the proxy's queue; returns the text."""
    model = model or DEFAULT_MODEL
    key = await _web_key(user_id)
    cfg = api_routes._resolve_model(await get_model_by_id(DB_PATH, model))
    result = await api_routes._handle_blocking(
        None, {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        model, messages, temperature, cfg, key, user_id, api_routes._is_admin_key(key),
        key.get("priority", 1), 0.0)
    return result["choices"][0]["message"]["content"]


# --- long-term memory ---

def _parse_memory_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _transcript(msgs: list[dict], name: str, username: str, start: str = "") -> tuple[str, dict[int, str]]:
    """Numbered, time-marked transcript lines, plus the story time in force at each message."""
    lines, story, story_at = [], "", {}
    for m in msgs:
        stamp = _stamp(m["created_at"])
        if m.get("story_at"):
            story = _clock_text(m["story_at"], start)
            stamp += f" | story time {story}"
        if m["role"] == "mark":
            story = f"{m['content']}, {story}" if m.get("story_at") else m["content"]
            lines.append(f"[#{m['id']} | {stamp}] --- Story time: {m['content']} ---")
        else:
            who = name if m["role"] == "assistant" else username
            lines.append(f"[#{m['id']} | {stamp}] {who}: {m['content']}")
        story_at[m["id"]] = story
    return "\n".join(lines), story_at


def _check_memory(item, older: list[dict]) -> tuple[str, dict, str] | None:
    """Keep a proposed memory only if its quote is found word for word in the
    transcript. Returns (fact, source message, quote) or None."""
    if not isinstance(item, dict):
        return None
    fact = str(item.get("fact") or "").strip()[:300]
    quote = str(item.get("quote") or "").strip()[:300]
    nq = _norm(quote)
    if not fact or len(nq) < 8:
        return None
    try:
        claimed = int(str(item.get("msg") or "").lstrip("#"))
    except ValueError:
        claimed = 0
    by_id = {m["id"]: m for m in older if m["role"] != "mark"}
    candidates = ([by_id[claimed]] if claimed in by_id else []) + list(by_id.values())
    for m in candidates:
        if nq in _norm(m["content"]):
            # Store the words as they were written, not the model's copy of them.
            words = re.findall(r"[\w']+", quote)
            exact = re.search(r"[^\w']+".join(re.escape(w) for w in words), m["content"], re.IGNORECASE) if words else None
            return fact, m, exact.group(0) if exact else quote
    return None


async def compact_conversation(conv_id: int, user_id: int, username: str):
    """Fold messages that fell out of the context window into the scene summary,
    and save new lasting facts as character memories. Every memory must quote the
    message it comes from word for word, or it is thrown away."""
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    history = await _rows("SELECT id, role, content, story_at, created_at FROM messages WHERE conversation_id = ? "
                          "ORDER BY id", (conv_id,))
    in_window = len(_history_window(history))
    older = [m for m in history[:len(history) - in_window] if m["id"] > cv["summarized_upto"]]
    if len(older) < COMPACT_EVERY:
        return
    known = [m["content"] for m in await _rows("SELECT content FROM memories WHERE character_id = ? ORDER BY id",
                                                (c["id"],))]
    start = await _scene_start(conv_id)
    transcript, story_at = _transcript(older, c["name"], username, start)
    while len(transcript) > 16000 and len(older) > COMPACT_EVERY:
        older = older[:len(older) * 3 // 4]  # fold the rest on the next pass instead of truncating it
        transcript, story_at = _transcript(older, c["name"], username, start)
    messages = [
        {"role": "system", "content":
            "You keep exact continuity notes for an ongoing role-play. Use only what the transcript and the story "
            "so far actually say: never guess, embellish, merge characters or change who said or did what. "
            "Each transcript line starts with [#message id | date and time]. Reply with JSON only, in this shape: "
            '{"summary": "...", "memories": [{"fact": "...", "quote": "...", "msg": 123}]}. '
            '"summary" is the updated story so far: the earlier summary followed by the new events, in order, one '
            "short line per event, each starting with its time mark in brackets, like [Oct 18, 2026 9:04 PM] or, "
            "its in-universe story time, like [Day 2, Sat Oct 17, 02:45 AM]. Keep names, numbers, places and promises "
            f'exactly as written. "memories" lists up to 5 NEW lasting facts {c["name"]} would remember about '
            f"{username} or their relationship (names, preferences, secrets, promises, turning points). For each, "
            '"fact" is one short sentence, "quote" is the exact words copied from the transcript that prove it '
            '(at least a few words, copied character for character), and "msg" is that line\'s message id. '
            "Leave out anything already in the known memories, and anything you cannot quote."},
        {"role": "user", "content":
            f"Story so far:\n{cv['summary'] or '(nothing yet)'}\n\nKnown memories:\n"
            + ("\n".join(f"- {k}" for k in known) or "(none)")
            + f"\n\nNew part of the transcript:\n{transcript}"},
    ]
    try:
        data = _parse_memory_json(await _complete(user_id, messages, 0.1, 1200))
    except Exception:
        return
    if not data:
        return
    summary = str(data.get("summary") or "").strip()[:6000]
    # Re-read before writing: a rewind while the model was working invalidates this pass.
    still = await _row("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND id BETWEEN ? AND ?",
                       (conv_id, older[0]["id"], older[-1]["id"]))
    if not still or still["n"] != len(older):
        return
    now = time.time()
    await _exec("UPDATE conversations SET summary = ?, summarized_upto = ? WHERE id = ?",
                (summary or cv["summary"], older[-1]["id"], conv_id))
    seen = {_norm(k) for k in known}
    for item in (data.get("memories") or [])[:5]:
        checked = _check_memory(item, older)
        if not checked:
            continue
        fact, src, quote = checked
        if _norm(fact) in seen or mentions_minor(fact):
            continue
        seen.add(_norm(fact))
        await _exec("""INSERT INTO memories (character_id, content, source, conversation_id, message_id, quote,
                       said_at, story_time, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (c["id"], fact, "auto", conv_id, src["id"], quote, src["created_at"],
                     story_at.get(src["id"], ""), now))


@router.get("/characters/{cid}/memories")
async def list_memories(cid: int, s: dict = Depends(_session)):
    return JSONResponse(await _rows("SELECT id, content, source, conversation_id, message_id, quote, said_at, "
                                    "story_time, created_at FROM memories WHERE character_id = ? "
                                    "ORDER BY COALESCE(said_at, created_at) DESC, id DESC", (cid,)))


@router.post("/characters/{cid}/memories")
async def add_memory(cid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    text = str(body.get("content") or "").strip()[:300]
    if not text:
        return _err(400, "Write the memory first.")
    if mentions_minor(text):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    mid = await _exec("INSERT INTO memories (character_id, content, source, created_at) VALUES (?,?,?,?)",
                      (cid, text, "user", time.time()))
    return JSONResponse({"id": mid})


@router.delete("/memories/{mid}")
async def delete_memory(mid: int, s: dict = Depends(_session)):
    await _exec("DELETE FROM memories WHERE id = ?", (mid,))
    return JSONResponse({"ok": True})


@router.put("/conversations/{conv_id}/summary")
async def edit_summary(conv_id: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    await _exec("UPDATE conversations SET summary = ? WHERE id = ?", (str(body.get("summary") or "")[:4000], conv_id))
    return JSONResponse({"ok": True})


@router.delete("/characters/{cid}")
async def delete_character(cid: int, s: dict = Depends(_session)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE conversation_id IN "
                         "(SELECT id FROM conversations WHERE character_id = ?)", (cid,))
        await db.execute("DELETE FROM conversations WHERE character_id = ?", (cid,))
        await db.execute("DELETE FROM novel_cast WHERE character_id = ?", (cid,))
        await db.execute("DELETE FROM memories WHERE character_id = ?", (cid,))
        await db.execute("DELETE FROM characters WHERE id = ?", (cid,))
        await db.commit()
    return JSONResponse({"ok": True})


# --- roleplay chats ---

@router.get("/conversations")
async def conversations(s: dict = Depends(_session)):
    return JSONResponse(await _rows(
        """SELECT cv.id, cv.character_id, cv.novel_id, cv.updated_at, c.name, c.avatar, c.color, n.title AS novel_title,
             (SELECT content FROM messages m WHERE m.conversation_id = cv.id ORDER BY m.id DESC LIMIT 1) AS last
           FROM conversations cv JOIN characters c ON c.id = cv.character_id
           LEFT JOIN novels n ON n.id = cv.novel_id
           ORDER BY cv.updated_at DESC LIMIT 200"""))


@router.post("/conversations")
async def start_conversation(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    c = await _row("SELECT * FROM characters WHERE id = ?", (int(body.get("character_id") or 0),))
    if not c:
        return _err(404, "Character not found")
    novel_id = body.get("novel_id") or None
    if not body.get("new"):
        existing = await _row("SELECT id FROM conversations WHERE character_id = ? AND novel_id IS ? "
                              "ORDER BY updated_at DESC LIMIT 1", (c["id"], novel_id))
        if existing:
            return JSONResponse({"id": existing["id"]})
    now = time.time()
    try:
        start = datetime.fromisoformat(str(body["story_start"])) if body.get("story_start") else datetime.now()
    except ValueError:
        return _err(400, "That story start time isn't a valid date and time.")
    cv = await _exec("INSERT INTO conversations (character_id, novel_id, created_at, updated_at) VALUES (?,?,?,?)",
                     (c["id"], novel_id, now, now))
    await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                (cv, "assistant", c["greeting"], _iso(start), now))
    return JSONResponse({"id": cv})


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: int, s: dict = Depends(_session)):
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    novel = await _row("SELECT id, title FROM novels WHERE id = ?", (cv["novel_id"],)) if cv["novel_id"] else None
    msgs = await _rows("SELECT id, role, content, story_at, created_at FROM messages WHERE conversation_id = ? "
                       "ORDER BY id", (conv_id,))
    upto = next((m["created_at"] for m in msgs if m["id"] == cv["summarized_upto"]), None)
    return JSONResponse({"id": conv_id, "character": c, "novel": novel, "messages": msgs, "summary": cv["summary"],
                         "summary_upto": upto, "clock": _iso(await _scene_clock(cv)),
                         "start": await _scene_start(conv_id)})



@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: int, s: dict = Depends(_session)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        await db.commit()
    return JSONResponse({"ok": True})


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: int, s: dict = Depends(_session)):
    """Delete a message and everything after it, to rewind or regenerate."""
    m = await _row("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if not m:
        return _err(404, "Message not found")
    await _exec("DELETE FROM messages WHERE conversation_id = ? AND id >= ?", (m["conversation_id"], msg_id))
    # Facts learned from rewound messages never happened.
    await _exec("DELETE FROM memories WHERE source = 'auto' AND conversation_id = ? AND message_id >= ?",
                (m["conversation_id"], msg_id))
    # Rewinding past summarized messages invalidates the scene summary.
    await _exec("UPDATE conversations SET summary = '', summarized_upto = 0 WHERE id = ? AND summarized_upto >= ?",
                (m["conversation_id"], msg_id))
    return JSONResponse({"ok": True})


@router.get("/time-rules")
async def get_time_rules(s: dict = Depends(_session)):
    return JSONResponse({**await _time_settings(), "defaults": DEFAULT_TIME_ACTIONS, "paces": PACES})


@router.put("/time-rules")
async def save_time_rules(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    try:
        pace = float(body.get("pace") or 1.0)
        actions = []
        for a in (body.get("actions") or [])[:40]:
            label = str(a.get("label") or "").strip()[:80]
            lo, hi = float(a.get("min") or 0), float(a.get("max") or 0)
            if not label:
                continue
            if not (0 <= lo <= hi <= MAX_SKIP_MINUTES):
                return _err(400, f"Check the minutes for “{label}”: the minimum must be 0 or more and not above the maximum.")
            actions.append({"label": label, "min": lo, "max": hi, "per": str(a.get("per") or "").strip()[:30],
                            "context": str(a.get("context") or "").strip()[:200]})
    except (TypeError, ValueError):
        return _err(400, "Minutes must be numbers.")
    if pace not in PACES:
        return _err(400, "Pick a pace of 0.5x, 1.0x or 1.5x.")
    if not actions:
        return _err(400, "Keep at least one action.")
    await _exec("INSERT INTO studio_settings (key, value) VALUES ('time_rules', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps({"pace": pace, "actions": actions}),))
    return JSONResponse({"ok": True})


@router.post("/conversations/{conv_id}/marks")
async def add_mark(conv_id: int, request: Request, s: dict = Depends(_session)):
    """Add a story-time mark to a scene: set the in-universe clock ("set": an ISO date
    and time), skip it ahead ("skip_minutes"), or just label the moment ("story_time")."""
    body = await request.json()
    text = str(body.get("story_time") or "").strip()[:120]
    if mentions_minor(text):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    if not (text or body.get("set") or body.get("skip_minutes")):
        return _err(400, "Set a time, a skip or a label first.")
    clock = await _scene_clock(cv)
    try:
        if body.get("set"):
            clock = datetime.fromisoformat(str(body["set"]))
        skip = int(float(body.get("skip_minutes") or 0))
    except ValueError:
        return _err(400, "That isn't a valid date, time or number of minutes.")
    if not 0 <= skip <= MAX_SKIP_MINUTES * 4:
        return _err(400, "Skip ahead by up to four weeks at a time.")
    clock += timedelta(minutes=skip)
    if not text:
        if skip:
            h, m = divmod(skip, 60)
            text = "Time skip: " + " ".join(p for p in (f"{h} hours" if h else "", f"{m} minutes" if m else "") if p)
        else:
            text = "Clock set"  # a "set" with no label
    now = time.time()
    mid = await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                      (conv_id, "mark", text, _iso(clock), now))
    await _exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
    return JSONResponse({"id": mid, "role": "mark", "content": text, "story_at": _iso(clock), "created_at": now})


@router.delete("/marks/{msg_id}")
async def delete_mark(msg_id: int, s: dict = Depends(_session)):
    await _exec("DELETE FROM messages WHERE id = ? AND role = 'mark'", (msg_id,))
    return JSONResponse({"ok": True})


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    text = (body.get("content") or "").strip()[:6000]
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    now = time.time()
    clock = await _scene_clock(cv)

    if text:  # an empty message means "regenerate the reply to the last message"
        if mentions_minor(text):
            return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                    (conv_id, "user", text, _iso(clock), now))
        if is_crisis(text):
            await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) "
                        "VALUES (?,?,?,?,?)", (conv_id, "assistant", CRISIS_RESPONSE, _iso(clock), now))
            return JSONResponse({"crisis": True, "content": CRISIS_RESPONSE})
    await _exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))

    system = (
        f"You are {c['name']}, a {c['age']}-year-old {c['gender']} character in an interactive dark romance "
        f"role-play with {s['username']}. Stay in character. Write only {c['name']}'s replies: speech plus "
        "short *italic actions*, 1 to 3 paragraphs, and never speak or act for the user. " + LIFELIKE_RULES + " Dark themes, tension "
        "and explicit adult content between consenting adult characters are allowed in this private story.\n\n"
        f"Personality and background: {c['persona']}\nAppearance: {c['appearance']}\n"
    )
    if cv["novel_id"]:
        novel, cast = await _novel_with_cast(cv["novel_id"])
        if novel:
            system += "\nThis role-play is a scene for the user's novel. Story bible:\n" + _bible(novel, cast) + "\n"
    # User-written memories always go in; the newest automatic ones fill the rest.
    mems = await _rows("""SELECT * FROM (SELECT content, said_at, story_time, created_at, source, id FROM memories
                          WHERE character_id = ? ORDER BY source = 'user' DESC, id DESC LIMIT ?)
                          ORDER BY COALESCE(said_at, created_at), id""", (c["id"], MAX_MEMORIES_IN_PROMPT))
    if mems:
        system += (f"\nThings {c['name']} remembers about {s['username']}, oldest first. These are facts:\n"
                   + "\n".join(f"- [{_stamp(m['said_at'] or m['created_at'])}"
                                + (f" | story time: {m['story_time']}" if m["story_time"] else "")
                                + f"] {m['content']}" for m in mems) + "\n")
    if cv["summary"]:
        system += f"\nThis scene so far (earlier messages, summarized in order):\n{cv['summary']}\n"

    history = await _rows("SELECT id, role, content, story_at, created_at FROM messages WHERE conversation_id = ? "
                          "ORDER BY id", (conv_id,))
    window = _history_window(history)
    first_in_window = history[len(history) - len(window)]["id"] if window else 0
    recalled = await _recall(c, conv_id, first_in_window, text or (history[-1]["content"] if history else ""),
                             s["username"])
    if recalled:
        system += ("\nExact lines from earlier, word for word, with when they were said:\n"
                   + "\n".join(f"[{_stamp(m['created_at'])}"
                                + (f" | story time {_clock_text(m['story_at'])}" if m["story_at"] else "")
                                + f"] {m['who']}: {m['content']}" for m in recalled) + "\n")
    marks = [m for m in history if m["role"] == "mark"]
    start = next((m["story_at"] for m in history if m["story_at"]), _iso(clock))
    tset = await _time_settings()
    system += (f"\nCurrent in-universe time: {_clock_text(_iso(clock), start)}, {_daypart(clock)}, "
               f"moon: {_moon(clock).lower()}"
               + (f" (latest story mark: {marks[-1]['content']})" if marks else "") + "\n\n"
               + _time_rules(tset["actions"]) + "\n")
    system += (f"\nContinuity: keep every detail consistent with the memories, summary and exact lines above "
               "(names, dates, places, promises, who did what). Never invent past events that aren't there; if "
               f"{c['name']} isn't sure, they stay vague instead of making it up.\n\n" + SAFETY_RULES)

    messages = [{"role": "system", "content": system}] + _chat_turns(window, start)

    async def save(reply):
        reply, minutes = _elapsed(reply, float(tset["pace"]))
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                    (conv_id, "assistant", reply, _iso(clock + timedelta(minutes=minutes)), time.time()))
        asyncio.create_task(compact_conversation(conv_id, s["user_id"], s["username"]))

    model = body.get("model_id") or DEFAULT_MODEL
    return await _stream(request, s, model, messages, 0.9, 450, save)


# --- novels ---

NOVEL_FIELDS = ("title", "premise", "tropes", "setting", "pov", "tone", "heat", "style_notes", "model_id")
NOVEL_DEFAULTS = {"pov": "first person, present tense", "tone": "dark, obsessive, slow-burn", "heat": "explicit"}


@router.get("/novels")
async def novels(s: dict = Depends(_session)):
    return JSONResponse(await _rows(
        """SELECT n.*, (SELECT COUNT(*) FROM chapters ch WHERE ch.novel_id = n.id) AS chapter_count,
             (SELECT COALESCE(SUM(LENGTH(ch.content) - LENGTH(REPLACE(ch.content, ' ', '')) + 1), 0)
              FROM chapters ch WHERE ch.novel_id = n.id AND ch.content != '') AS word_count
           FROM novels n ORDER BY n.updated_at DESC"""))


@router.post("/novels")
async def save_novel(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    f = {k: str(body.get(k) or "").strip()[:6000] or NOVEL_DEFAULTS.get(k, "") for k in NOVEL_FIELDS}
    if not f["title"]:
        return _err(400, "Give the novel a title.")
    if any(mentions_minor(f[k]) for k in ("premise", "tropes", "setting", "style_notes")):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    now = time.time()
    nid = body.get("id")
    if nid:
        sets = ", ".join(f"{k} = ?" for k in NOVEL_FIELDS)
        await _exec(f"UPDATE novels SET {sets}, updated_at = ? WHERE id = ?",
                    (*[f[k] or None if k == "model_id" else f[k] for k in NOVEL_FIELDS], now, int(nid)))
    else:
        nid = await _exec(
            f"INSERT INTO novels ({', '.join(NOVEL_FIELDS)}, created_at, updated_at) VALUES ({', '.join('?' * len(NOVEL_FIELDS))}, ?, ?)",
            (*[f[k] or (None if k == "model_id" else "") for k in NOVEL_FIELDS], now, now))
        await _exec("INSERT INTO chapters (novel_id, position, title, content, updated_at) VALUES (?,?,?,?,?)",
                    (nid, 1, "Chapter 1", "", now))
    if isinstance(body.get("cast"), list):
        await _exec("DELETE FROM novel_cast WHERE novel_id = ?", (int(nid),))
        for item in body["cast"]:
            await _exec("INSERT OR REPLACE INTO novel_cast (novel_id, character_id, role) VALUES (?,?,?)",
                        (int(nid), int(item.get("character_id")), str(item.get("role") or "")[:80]))
    return JSONResponse({"id": int(nid)})


@router.get("/novels/{nid}")
async def get_novel(nid: int, s: dict = Depends(_session)):
    novel, cast = await _novel_with_cast(nid)
    if not novel:
        return _err(404, "Novel not found")
    chapters = await _rows("SELECT id, position, title, summary, LENGTH(content) AS length FROM chapters "
                           "WHERE novel_id = ? ORDER BY position", (nid,))
    novel["cast"] = [{"character_id": c["id"], "name": c["name"], "role": c["role"], "avatar": c["avatar"],
                      "color": c["color"]} for c in cast]
    novel["chapters"] = chapters
    return JSONResponse(novel)


@router.delete("/novels/{nid}")
async def delete_novel(nid: int, s: dict = Depends(_session)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chapters WHERE novel_id = ?", (nid,))
        await db.execute("DELETE FROM novel_cast WHERE novel_id = ?", (nid,))
        await db.execute("UPDATE conversations SET novel_id = NULL WHERE novel_id = ?", (nid,))
        await db.execute("DELETE FROM novels WHERE id = ?", (nid,))
        await db.commit()
    return JSONResponse({"ok": True})


@router.get("/novels/{nid}/export")
async def export_novel(nid: int, s: dict = Depends(_session)):
    novel = await _row("SELECT * FROM novels WHERE id = ?", (nid,))
    if not novel:
        return _err(404, "Novel not found")
    chapters = await _rows("SELECT title, content FROM chapters WHERE novel_id = ? ORDER BY position", (nid,))
    text = f"# {novel['title']}\n\n" + "\n\n".join(f"## {c['title']}\n\n{c['content']}" for c in chapters) + "\n"
    safe = "".join(ch for ch in novel["title"] if ch.isalnum() or ch in " -_").strip() or "novel"
    return StreamingResponse(iter([text.encode()]), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})


# --- chapters ---

@router.post("/novels/{nid}/chapters")
async def add_chapter(nid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    pos = (await _row("SELECT COALESCE(MAX(position), 0) AS p FROM chapters WHERE novel_id = ?", (nid,)))["p"] + 1
    cid = await _exec("INSERT INTO chapters (novel_id, position, title, content, updated_at) VALUES (?,?,?,?,?)",
                      (nid, pos, str(body.get("title") or f"Chapter {pos}")[:120], "", time.time()))
    return JSONResponse({"id": cid})


@router.get("/chapters/{cid}")
async def get_chapter(cid: int, s: dict = Depends(_session)):
    ch = await _row("SELECT * FROM chapters WHERE id = ?", (cid,))
    return JSONResponse(ch) if ch else _err(404, "Chapter not found")


@router.put("/chapters/{cid}")
async def save_chapter(cid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    ch = await _row("SELECT * FROM chapters WHERE id = ?", (cid,))
    if not ch:
        return _err(404, "Chapter not found")
    title = str(body.get("title", ch["title"]))[:120]
    summary = str(body.get("summary", ch["summary"]))[:4000]
    content = str(body.get("content", ch["content"]))
    now = time.time()
    await _exec("UPDATE chapters SET title=?, summary=?, content=?, updated_at=? WHERE id=?",
                (title, summary, content, now, cid))
    await _exec("UPDATE novels SET updated_at = ? WHERE id = ?", (now, ch["novel_id"]))
    return JSONResponse({"ok": True, "updated_at": now})


@router.post("/chapters/{cid}/move")
async def move_chapter(cid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    ch = await _row("SELECT * FROM chapters WHERE id = ?", (cid,))
    if not ch:
        return _err(404, "Chapter not found")
    step = -1 if body.get("direction") == "up" else 1
    other = await _row("SELECT * FROM chapters WHERE novel_id = ? AND position = ?",
                       (ch["novel_id"], ch["position"] + step))
    if other:
        await _exec("UPDATE chapters SET position = ? WHERE id = ?", (ch["position"], other["id"]))
        await _exec("UPDATE chapters SET position = ? WHERE id = ?", (other["position"], cid))
    return JSONResponse({"ok": True})


@router.delete("/chapters/{cid}")
async def delete_chapter(cid: int, s: dict = Depends(_session)):
    ch = await _row("SELECT * FROM chapters WHERE id = ?", (cid,))
    if ch:
        await _exec("DELETE FROM chapters WHERE id = ?", (cid,))
        await _exec("UPDATE chapters SET position = position - 1 WHERE novel_id = ? AND position > ?",
                    (ch["novel_id"], ch["position"]))
    return JSONResponse({"ok": True})


# --- AI writing tools ---

@router.post("/novels/{nid}/assist")
async def assist(nid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    mode = body.get("mode") if body.get("mode") in ASSIST_MODES else "continue"
    instruction = str(body.get("instruction") or "").strip()[:3000]
    selection = str(body.get("selection") or "").strip()[:8000]
    if mentions_minor(instruction) or mentions_minor(selection):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    novel, cast = await _novel_with_cast(nid)
    if not novel:
        return _err(404, "Novel not found")
    chapter = await _row("SELECT * FROM chapters WHERE id = ? AND novel_id = ?", (int(body.get("chapter_id") or 0), nid))

    earlier = []
    if chapter:
        for ch in await _rows("SELECT title, summary FROM chapters WHERE novel_id = ? AND position < ? ORDER BY position",
                              (nid, chapter["position"])):
            earlier.append(f"- {ch['title']}: {ch['summary'] or '(no summary yet)'}")

    system = (
        "You are a skilled co-author of dark romance fiction, writing for an adult author's private manuscript. "
        "Write vivid, emotionally intense, realistic prose with strong tension, morally grey love interests and sharp dialogue. "
        "Describe every character, human or supernatural, in lifelike, photorealistic physical detail; never cartoonish or anime-like. "
        "Dark themes and explicit sexual content between consenting adult characters are allowed and should match "
        "the heat level in the story bible. Match the novel's point of view, tense and voice exactly. Never add "
        "notes, headings or commentary unless asked: return only the requested text.\n\n"
        "Story bible:\n" + _bible(novel, cast) + "\n\n" + SAFETY_RULES
    )
    parts = []
    if earlier:
        parts.append("Earlier chapters:\n" + "\n".join(earlier))
    if chapter:
        text = chapter["content"][-CHAPTER_CONTEXT_CHARS:]
        parts.append(f"Current chapter, \"{chapter['title']}\" (most recent text):\n\"\"\"\n{text or '(empty so far)'}\n\"\"\"")
    if selection:
        parts.append(f"Selected passage:\n\"\"\"\n{selection}\n\"\"\"")
    parts.append("Task: " + ASSIST_MODES[mode])
    if instruction:
        parts.append("Instruction from the author: " + instruction)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]

    model = novel["model_id"] or DEFAULT_MODEL
    temperature = 1.0 if mode == "brainstorm" else 0.85
    return await _stream(request, s, model, messages, temperature,
                         ASSIST_LENGTH.get(body.get("length"), ASSIST_LENGTH["medium"]))


@router.post("/chapters/{cid}/summarize")
async def summarize_chapter(cid: int, request: Request, s: dict = Depends(_session)):
    """Write a short summary of a chapter, saved so later chapters keep continuity."""
    ch = await _row("SELECT * FROM chapters WHERE id = ?", (cid,))
    if not ch or not ch["content"].strip():
        return _err(400, "This chapter has no text to summarize yet.")
    messages = [
        {"role": "system", "content": "Summarize this chapter of a novel in 3 to 5 sentences for the author's "
                                      "continuity notes: key events, relationship changes, open threads. Plain prose only."},
        {"role": "user", "content": ch["content"][-20_000:]},
    ]

    async def save(summary):
        await _exec("UPDATE chapters SET summary = ? WHERE id = ?", (summary[:4000], cid))

    novel = await _row("SELECT model_id FROM novels WHERE id = ?", (ch["novel_id"],))
    return await _stream(request, s, (novel or {}).get("model_id") or DEFAULT_MODEL, messages, 0.3, 300, save)
