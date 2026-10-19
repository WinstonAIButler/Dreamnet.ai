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
from fastapi.responses import JSONResponse, Response, StreamingResponse

import app.api_routes as api_routes
import app.option_images as option_images
import app.places as places
import app.portraits as portraits
import app.profile as profiles
import app.voices as voices
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
    profile TEXT NOT NULL DEFAULT '{}',         -- detailed profile: {field: {"value", "secret"}}
    voice TEXT NOT NULL DEFAULT '',             -- read-aloud voice id, empty = default for gender
    voice_tone TEXT NOT NULL DEFAULT 'natural',
    voice_rate INTEGER NOT NULL DEFAULT 0,      -- speed, percent faster or slower
    voice_engine TEXT NOT NULL DEFAULT 'edge',  -- edge (online) or voicestudio (on this computer)
    voice_profile TEXT NOT NULL DEFAULT '',     -- saved VoiceStudio voice id; empty = designed from the character
    voice_style TEXT NOT NULL DEFAULT '',       -- accent preset JSON {accent, age, gender, sound}; empty = by country
    color TEXT NOT NULL DEFAULT '#F1916D',
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

-- A storyline is one continuity: its scenes share a clock, a Day count, exact recall
-- and the characters' memories. Separate storylines never see each other.
CREATE TABLE IF NOT EXISTS storylines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    premise TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    novel_id INTEGER,                 -- set = the roleplay knows that story bible
    storyline_id INTEGER,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',           -- rolling "story so far" of older turns
    summarized_upto INTEGER NOT NULL DEFAULT 0, -- last message id folded into summary
    time_rules TEXT NOT NULL DEFAULT '',        -- JSON list of extra or overriding actions for this scene
    location TEXT NOT NULL DEFAULT '',          -- where the scene starts
    situation TEXT NOT NULL DEFAULT '',         -- the scenario: what's going on as it starts (optional)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Lasting facts a character remembers across every scene.
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',        -- auto | user
    storyline_id INTEGER,                       -- NULL = remembered in every storyline
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
    time_override REAL,               -- user message: minutes the reply must cover, set with [Time: ...]
    image TEXT NOT NULL DEFAULT '',   -- scene picture URL for this moment
    image_prompt TEXT NOT NULL DEFAULT '',
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
    dict(name="Dante Moretti", voice="en-US-ChristopherNeural", voice_tone="deep", age=34, gender="male", color="#c8697a",
         tagline="Heir to a crime family who never asks twice",
         appearance="Tall, dark hair pushed back, a scar through one eyebrow, tailored black suits, a signet ring.",
         persona="Dante is the cold, controlled heir of the Moretti family. Possessive, calculating and quietly "
                 "dangerous, he speaks softly and rarely raises his voice because he never needs to. He keeps "
                 "his promises and his grudges, and his rare tenderness is reserved for the one person he has "
                 "decided is his.",
         greeting="*doesn't look up from the ledger* You walked into the wrong room, cara. Close the door. Now tell me why I shouldn't keep you here.",
         tags="mafia,possessive,morally grey"),
    dict(name="Nikolai Volkov", voice="en-GB-ThomasNeural", voice_tone="deep", age=37, gender="male", color="#19305C",
         tagline="Bratva enforcer who was sent to collect your debt",
         appearance="Six foot four, close-cropped blond hair, pale grey eyes, tattooed hands, a worn leather coat.",
         persona="Nikolai is the quiet enforcer of a Russian crime family in Chicago. Patient, observant and "
                 "unsettlingly calm, he speaks little and means every word. He grew up with nothing, trusts no one, "
                 "and is thrown by how badly he wants to protect the person he was sent to frighten.",
         greeting="*sets your unpaid ledger on the kitchen table and sits down without being asked* Relax. If I wanted to hurt you, you'd already know. We need to talk about your brother.",
         tags="bratva,stalker,protective"),
    dict(name="Lucien Vale", voice="en-GB-RyanNeural", voice_tone="low", age=312, gender="male", color="#413B61",
         tagline="Vampire lord who has waited centuries for you",
         appearance="Tall and lean, porcelain skin with faint blue veins at the temples, sharp cheekbones, long black "
                    "hair tied back, dark eyes that catch the light red, a fitted black wool coat over an open shirt.",
         persona="Lucien is an ancient vampire who passes for a reclusive collector in modern-day New Orleans. "
                 "Elegant, melancholy and predatory, he speaks with old-world formality, is fascinated by the person "
                 "in front of him, and fights his hunger as much as he indulges it.",
         greeting="*steps out of the shadow of the bookshelves, cold air following him* Three hundred years, and you still arrive late. Come closer. I don't bite... unless asked.",
         tags="vampire,gothic,obsessive"),
    dict(name="Rhys Callahan", voice="en-IE-ConnorNeural", voice_tone="low", age=36, gender="male", color="#2b4a85",
         tagline="Ex-mercenary bodyguard who hates this job",
         appearance="Broad, scarred knuckles, buzzed hair, grey eyes, black tactical jacket, a gun he never hides.",
         persona="Rhys is a gruff former mercenary hired to protect someone he finds infuriating. Blunt, sarcastic and "
                 "fiercely protective, he hides a violent past and a soft spot he would rather die than admit.",
         greeting="*checks the window, then you* Rule one: you go nowhere without me. Rule two: stop looking at me like that.",
         tags="bodyguard,enemies to lovers,protective"),
    dict(name="Adrian Kross", voice="en-US-BrianNeural", voice_tone="natural", age=38, gender="male", color="#AE7DAC",
         tagline="Billionaire rival who bought your company to own you",
         appearance="Silver-touched dark hair, clean-shaven, ice-blue eyes, immaculate charcoal suits.",
         persona="Adrian is a ruthless tech billionaire who wins everything he wants. Arrogant, brilliant and "
                 "manipulative, he enjoys verbal duels and slowly reveals an obsession that started years ago.",
         greeting="*signs the last page and slides it across the desk* Congratulations. You work for me now. Sit down, we have a lot to discuss.",
         tags="billionaire,rivals,power play"),
    dict(name="Mara Voss", voice="en-GB-SoniaNeural", voice_tone="low", age=29, gender="female", color="#F1916D",
         tagline="Assassin sent to kill you, who didn't",
         appearance="Black hair in a sharp braid, dark eyes, a knife scar on her collarbone, a fitted leather jacket.",
         persona="Mara is a cold, elite assassin who broke contract for the first time when she met her target. "
                 "Guarded, dry-humoured and deadly, she trusts no one and is furious that she cares.",
         greeting="*the blade rests against your throat, then lowers* You should be dead. Give me one reason I didn't finish the job.",
         tags="assassin,dangerous,slow-burn"),
    dict(name="Sienna Hart", voice="en-US-AvaNeural", voice_tone="natural", age=28, gender="female", color="#d9876a",
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


def _merge_actions(base: list[dict], scene: list[dict]) -> list[dict]:
    """Scene rules replace a global rule with the same name and add the rest, marked as scene rules."""
    names = {a["label"].strip().lower(): i for i, a in enumerate(base)}
    out = [dict(a) for a in base]
    for a in scene:
        a = {**a, "scene": True}
        i = names.get(a["label"].strip().lower())
        if i is None:
            out.append(a)
        else:
            out[i] = a
    return out


def _clean_actions(raw) -> list[dict]:
    """Validate an action-time table from the browser. Raises ValueError with a message for the user."""
    actions = []
    for a in (raw or [])[:40]:
        label = str(a.get("label") or "").strip()[:80]
        if not label:
            continue
        try:
            lo, hi = float(a.get("min") or 0), float(a.get("max") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"The minutes for “{label}” must be numbers.")
        if not (0 <= lo <= hi <= MAX_SKIP_MINUTES):
            raise ValueError(f"Check the minutes for “{label}”: the minimum must be 0 or more and not above the maximum.")
        actions.append({"label": label, "min": lo, "max": hi, "per": str(a.get("per") or "").strip()[:30],
                        "context": str(a.get("context") or "").strip()[:200]})
    return actions


_OVERRIDE = re.compile(r"\[\s*time\s*:\s*([^\]]+)\]", re.IGNORECASE)
_CLOCK_AT = re.compile(r"^(?:at\s+)?(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?$", re.IGNORECASE)


def _time_value(value: str, base: datetime) -> datetime | None:
    """Read a time a user typed, relative to ``base``: a clock time ("03:15 AM", "23:40")
    means the next time the clock reads that; a duration ("45 min", "+2 hours", "1h 30m") is added."""
    value = value.strip()
    m = _CLOCK_AT.match(value)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if ap and not 1 <= h <= 12 or h > 23 or mi > 59:
            return None
        if ap:
            h = h % 12 + (12 if ap.startswith("p") else 0)
        target = base.replace(hour=h, minute=mi, second=0)
        return target if target >= base else target + timedelta(days=1)
    units = _UNITS.findall(value)
    if not units:
        return None
    total = 0.0
    for n, unit in units:
        u = unit.lower()[0]
        total += float(n) * (1440 if u == "d" else 60 if u == "h" else 1 / 60 if u == "s" else 1)
    return base + timedelta(minutes=min(total, MAX_SKIP_MINUTES)) if total >= 0 else None


def _time_rules(actions: list[dict]) -> str:
    """The time protocol for the system prompt, built from the action-time table."""
    table = "\n".join(
        f"   - {a['label']}{' (this scene)' if a.get('scene') else ''}: " + (_span(a["min"]) if a["min"] == a["max"] else f"{_span(a['min'])} to {_span(a['max'])}")
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
        if "profile" not in cols:
            await db.execute("ALTER TABLE characters ADD COLUMN profile TEXT NOT NULL DEFAULT '{}'")
        if "voice" not in cols:
            await db.execute("ALTER TABLE characters ADD COLUMN voice TEXT NOT NULL DEFAULT ''")
            await db.execute("ALTER TABLE characters ADD COLUMN voice_tone TEXT NOT NULL DEFAULT 'natural'")
            await db.execute("ALTER TABLE characters ADD COLUMN voice_rate INTEGER NOT NULL DEFAULT 0")
        if "voice_engine" not in cols:
            await db.execute("ALTER TABLE characters ADD COLUMN voice_engine TEXT NOT NULL DEFAULT 'edge'")
            await db.execute("ALTER TABLE characters ADD COLUMN voice_profile TEXT NOT NULL DEFAULT ''")
        if "voice_style" not in cols:
            await db.execute("ALTER TABLE characters ADD COLUMN voice_style TEXT NOT NULL DEFAULT ''")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(conversations)")).fetchall()]
        if "summary" not in cols:
            await db.execute("ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            await db.execute("ALTER TABLE conversations ADD COLUMN summarized_upto INTEGER NOT NULL DEFAULT 0")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(messages)")).fetchall()]
        if "story_at" not in cols:
            await db.execute("ALTER TABLE messages ADD COLUMN story_at TEXT NOT NULL DEFAULT ''")
        if "time_override" not in cols:
            await db.execute("ALTER TABLE messages ADD COLUMN time_override REAL")
        if "image" not in cols:
            await db.execute("ALTER TABLE messages ADD COLUMN image TEXT NOT NULL DEFAULT ''")
            await db.execute("ALTER TABLE messages ADD COLUMN image_prompt TEXT NOT NULL DEFAULT ''")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(conversations)")).fetchall()]
        if "time_rules" not in cols:
            await db.execute("ALTER TABLE conversations ADD COLUMN time_rules TEXT NOT NULL DEFAULT ''")
        if "storyline_id" not in cols:
            await db.execute("ALTER TABLE conversations ADD COLUMN storyline_id INTEGER")
        if "location" not in cols:
            await db.execute("ALTER TABLE conversations ADD COLUMN location TEXT NOT NULL DEFAULT ''")
            await db.execute("ALTER TABLE conversations ADD COLUMN situation TEXT NOT NULL DEFAULT ''")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(memories)")).fetchall()]
        if "storyline_id" not in cols:
            await db.execute("ALTER TABLE memories ADD COLUMN storyline_id INTEGER")
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(memories)")).fetchall()]
        for col, kind in (("conversation_id", "INTEGER"), ("message_id", "INTEGER"),
                          ("quote", "TEXT NOT NULL DEFAULT ''"), ("said_at", "REAL"),
                          ("story_time", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {kind}")
        # Scenes from before storylines: one storyline per character, holding its scenes and memories.
        orphans = await (await db.execute(
            """SELECT cv.character_id, c.name, MIN(cv.created_at) FROM conversations cv
               JOIN characters c ON c.id = cv.character_id WHERE cv.storyline_id IS NULL
               GROUP BY cv.character_id""")).fetchall()
        for cid, name, created in orphans:
            sid = (await db.execute("INSERT INTO storylines (title, created_at, updated_at) VALUES (?,?,?)",
                                    (f"{name}'s story", created, time.time()))).lastrowid
            await db.execute("UPDATE conversations SET storyline_id = ? WHERE character_id = ? AND storyline_id IS NULL",
                             (sid, cid))
            await db.execute("""UPDATE memories SET storyline_id = COALESCE(
                                  (SELECT storyline_id FROM conversations WHERE id = memories.conversation_id), ?)
                                WHERE character_id = ? AND storyline_id IS NULL""", (sid, cid))
        await db.commit()
        n = (await (await db.execute("SELECT COUNT(*) FROM characters")).fetchone())[0]
        if n == 0:
            now = time.time()
            for c in SEED_CHARACTERS:
                await db.execute(
                    """INSERT INTO characters (name, age, gender, tagline, appearance, persona, greeting,
                       tags, avatar, color, voice, voice_tone, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (c["name"], c["age"], c["gender"], c["tagline"], c["appearance"], c["persona"],
                     c["greeting"], c["tags"], c.get("avatar", ""), c["color"], c.get("voice", ""),
                     c.get("voice_tone", "natural"), now, now))
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


async def _story_start(cv: dict) -> str:
    """In-universe time the storyline began (its first stamped message); Day 1 is that day."""
    first = await _row("""SELECT m.story_at FROM messages m JOIN conversations c ON c.id = m.conversation_id
                          WHERE (c.storyline_id = ? OR c.id = ?) AND m.story_at != '' ORDER BY m.id LIMIT 1""",
                       (cv["storyline_id"], cv["id"]))
    return first["story_at"] if first else ""


async def _storyline_clock(storyline_id: int) -> datetime | None:
    """Latest in-universe time in a storyline, where its next scene picks up."""
    last = await _row("""SELECT m.story_at FROM messages m JOIN conversations c ON c.id = m.conversation_id
                         WHERE c.storyline_id = ? AND m.story_at != '' ORDER BY m.id DESC LIMIT 1""", (storyline_id,))
    return datetime.fromisoformat(last["story_at"]) if last else None


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
                                                 "\u2014": "-", "\u2013": "-", "*": " ", "_": " ", "`": " "}))
    return re.sub(r"\s+", " ", text).strip(" .,!?\"'")


_STOP = set("""a an and are as at be been but by can could did do does for from had has have he her hers him his how i
if in into is it its just me my no not now of on or our out she so than that the their them then there they this
to too up us was we were what when where which who why will with would you your yours i'm i'll it's don't""".split())


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", text.lower()) if w not in _STOP}


async def _recall(cv: dict, older_ids_below: int, query: str, username: str) -> list[dict]:
    """Verbatim earlier messages that best match the new message: from this scene
    (before the context window) and from the storyline's other scenes, with any character."""
    words = _keywords(query)
    if not words:
        return []
    rows = await _rows(
        """SELECT m.id, m.conversation_id, m.role, m.content, m.story_at, m.created_at, ch.name FROM messages m
           JOIN conversations cv ON cv.id = m.conversation_id JOIN characters ch ON ch.id = cv.character_id
           WHERE (cv.storyline_id = ? OR cv.id = ?) AND m.role IN ('user', 'assistant')
             AND (m.conversation_id != ? OR m.id < ?)""",
        (cv["storyline_id"], cv["id"], cv["id"], older_ids_below))
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
        picked.append({**m, "content": text, "who": m["name"] if m["role"] == "assistant" else username})
    return sorted(picked, key=lambda m: m["id"])


def _character_fields(body: dict) -> dict:
    f = {k: str(body.get(k) or "").strip()[:4000] for k in
         ("name", "gender", "tagline", "appearance", "persona", "greeting", "tags", "color", "avatar")}
    f["age"] = body.get("age")
    f["profile"] = profiles.clean(body.get("profile"))
    f["voice"] = str(body.get("voice") or "")
    if f["voice"] and f["voice"] not in voices.VOICE_IDS and not f["voice"].startswith("device:"):
        f["voice"] = ""
    f["voice_tone"] = body.get("voice_tone") if body.get("voice_tone") in voices.TONES else "natural"
    try:
        f["voice_rate"] = max(-50, min(50, int(body.get("voice_rate") or 0)))
    except (TypeError, ValueError):
        f["voice_rate"] = 0
    f["voice_engine"] = body.get("voice_engine") if body.get("voice_engine") in voices.ENGINES else "edge"
    f["voice_profile"] = str(body.get("voice_profile") or "").strip()[:200]
    style = voices.clean_style(body.get("voice_style"))
    f["voice_style"] = json.dumps(style) if style else ""
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
    if not c:
        return _err(404, "Character not found")
    return JSONResponse({**c, "profile": profiles.clean(c["profile"])})


@router.get("/profile-fields")
async def profile_fields(s: dict = Depends(_session)):
    return JSONResponse([{"section": sec, "fields": [{"key": k, "label": l, "hint": h} for k, l, h in fields]}
                         for sec, fields in profiles.SECTIONS])


BASIC_FIELDS = ("name", "age", "gender", "tagline", "appearance", "persona", "greeting", "tags")


@router.post("/characters/ai-fill")
async def ai_fill(request: Request, s: dict = Depends(_session)):
    """Fill in a character with the AI. scope: "all" rewrites everything, "empty" fills only
    blank fields, or a single field key (basic or profile) rewrites just that one."""
    body = await request.json()
    cur = {k: str((body.get("character") or {}).get(k) or "").strip() for k in BASIC_FIELDS}
    prof = profiles.clean(body.get("profile"))
    idea = str(body.get("idea") or "").strip()[:1000]
    scope = str(body.get("scope") or "empty")
    if mentions_minor(idea, strict=True):
        return _err(400, "Characters can't be described as minors or as childlike.")
    if scope == "all":
        want_basic, want_prof = list(BASIC_FIELDS), list(profiles.FIELDS)
    elif scope == "empty":
        want_basic = [k for k in BASIC_FIELDS if not cur[k]]
        want_prof = [k for k in profiles.FIELDS if k not in prof]
    elif scope in BASIC_FIELDS or scope in profiles.FIELDS:
        want_basic = [scope] if scope in BASIC_FIELDS else []
        want_prof = [scope] if scope in profiles.FIELDS else []
    else:
        return _err(400, "Unknown field.")
    if not want_basic and not want_prof:
        return JSONResponse({"character": {}, "profile": {}})
    known = "\n".join(f"{k}: {v}" for k, v in cur.items() if v and k not in want_basic)
    known += "".join(f"\n{profiles.FIELDS[k]}{' (secret)' if v['secret'] else ''}: {v['value']}"
                     for k, v in prof.items() if k not in want_prof)
    shape = ", ".join(f'"{k}": "..."' for k in want_basic if k != "age") + (', "age": 30' if "age" in want_basic else "")
    prof_list = "\n".join(f"- {k}: {profiles.FIELDS[k]}" for k in want_prof)
    messages = [
        {"role": "system", "content":
            "You design characters for a private, adults-only dark romance novel. Every character is an adult aged "
            "18 or over, never childlike. " + LIFELIKE_RULES + " Be specific, vivid and consistent with everything "
            "already known about the character; invent concrete names, places, brands and numbers rather than "
            "staying vague. Reply with JSON only."},
        {"role": "user", "content":
            (f"Idea: {idea}\n\n" if idea else "")
            + (f"Already decided (keep consistent):\n{known}\n\n" if known.strip() else "")
            + "Return one JSON object" + (f" with {shape}" if shape else "")
            + (f'{", and" if shape else " with"} "profile": an object whose keys are these field ids, each '
               '{"value": "one to three sentences", "secret": true or false} (secret = true for things the '
               "character hides from the person they're falling for):\n" + prof_list if want_prof else "")
            + ('\n"persona" is personality and backstory in 3 to 5 sentences; "appearance" is 2 to 3 sentences of '
               'lifelike physical detail; "greeting" is an in-character opening line using *actions*, "speech" and '
               '`thoughts`; "tags" is 3 to 6 comma-separated tags; "gender" is male, female or non-binary.'
               if want_basic else "")},
    ]
    try:
        data = _parse_memory_json(await _complete(s["user_id"], messages, 0.85, 3000))
    except Exception as e:
        return _err(502, f"The AI didn't answer: {e}")
    if not data:
        return _err(502, "The AI's answer couldn't be read. Try again.")
    out_basic = {}
    for k in want_basic:
        if k in data and str(data[k]).strip():
            out_basic[k] = str(data[k]).strip()[:4000]
    if "age" in out_basic:
        try:
            out_basic["age"] = max(18, int(float(out_basic["age"])))
        except ValueError:
            out_basic.pop("age")
    if out_basic.get("gender") not in (None, "male", "female", "non-binary"):
        out_basic.pop("gender")
    out_prof = {k: v for k, v in profiles.clean(data.get("profile") or {}).items() if k in want_prof}
    texts = [str(v) for v in out_basic.values()] + [v["value"] for v in out_prof.values()]
    if any(mentions_minor(t, strict=True) for t in texts):
        return _err(400, "That came back describing someone underage, so it was thrown away. Try again.")
    return JSONResponse({"character": out_basic, "profile": out_prof})


@router.post("/characters")
async def save_character(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    f = _character_fields(body)
    if not f["name"] or not f["persona"]:
        return _err(400, "Give the character a name and a personality.")
    problem = check_character(f)
    if not problem and any(mentions_minor(v["value"], strict=True) for v in f["profile"].values()):
        problem = "Characters can't be described as minors or as childlike."
    if problem:
        return _err(400, problem)
    greeting = f["greeting"] or f"*looks at you* So. You're finally here."
    now = time.time()
    cid = body.get("id")
    if cid:
        await _exec("""UPDATE characters SET name=?, age=?, gender=?, tagline=?, appearance=?, persona=?,
                       greeting=?, tags=?, color=?, avatar=?, voice=?, voice_tone=?, voice_rate=?,
                       voice_engine=?, voice_profile=?, voice_style=?, profile=?, updated_at=?
                       WHERE id=?""",
                    (f["name"][:60], int(f["age"]), f["gender"] or "female", f["tagline"][:160], f["appearance"],
                     f["persona"], greeting, f["tags"][:200], f["color"] or "#F1916D", f["avatar"],
                     f["voice"], f["voice_tone"], f["voice_rate"], f["voice_engine"], f["voice_profile"], f["voice_style"],
                     json.dumps(f["profile"]), now, int(cid)))
        return JSONResponse({"id": int(cid)})
    cid = await _exec("""INSERT INTO characters (name, age, gender, tagline, appearance, persona, greeting,
                         tags, avatar, color, voice, voice_tone, voice_rate, voice_engine, voice_profile, voice_style,
                         profile, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (f["name"][:60], int(f["age"]), f["gender"] or "female", f["tagline"][:160], f["appearance"],
                       f["persona"], greeting, f["tags"][:200], f["avatar"], f["color"] or "#F1916D",
                       f["voice"], f["voice_tone"], f["voice_rate"], f["voice_engine"], f["voice_profile"], f["voice_style"],
                       json.dumps(f["profile"]), now, now))
    return JSONResponse({"id": cid})


@router.get("/voices")
async def list_voices(s: dict = Depends(_session)):
    return JSONResponse({"enabled": voices.enabled(), "regions": voices.REGIONS, "voices": voices.VOICES,
                         "tones": list(voices.TONES), "defaults": voices.DEFAULT_VOICE,
                         "voicestudio": await voices.voicestudio_status(api_routes.http_client),
                         "presets": voices.preset_catalog()})


@router.post("/tts")
async def tts(request: Request, s: dict = Depends(_session)):
    """Read text aloud in a voice: returns MP3. "mode" picks what to read: all, words_thoughts or words.
    With engine "voicestudio" it asks VoiceStudio first and falls back to the online voice."""
    body = await request.json()
    mode = body.get("mode") if body.get("mode") in voices.READ_MODES else "all"
    raw = str(body.get("text") or "")
    text = voices.speakable(raw, mode)
    if not text:
        return _err(400, "Nothing to read in that message.")
    voice, tone, rate = str(body.get("voice") or ""), str(body.get("tone") or "natural"), body.get("rate") or 0
    headers = {"Cache-Control": "private, max-age=86400"}
    pitch, tags = None, None
    style = voices.clean_style(body.get("style"))
    if style:  # accent preset: its own voice, pitch and speed (speed slider still adds on top)
        p = voices.preset(style, rate)
        voice, pitch, rate, tags = p["voice"], p["pitch"], p["rate"], p["vs_tags"]
        body["gender"] = style["gender"]
    # VoiceStudio only knows some accents; presets it can't do read through the online voice.
    if body.get("engine") == "voicestudio" and (not style or tags or body.get("profile")):
        try:
            age = int(body.get("age") or 30)
        except (TypeError, ValueError):
            age = 30
        try:
            audio = await voices.synthesize_voicestudio(
                api_routes.http_client, raw, mode, profile=str(body.get("profile") or ""),
                gender=str(body.get("gender") or "female"), age=age, tone=tone,
                region=str(body.get("region") or voice[:5] or "en-US"), rate=rate,
                seed_key=body.get("name") or body.get("character_id") or "", tags=tags)
            return Response(audio, media_type="audio/mpeg", headers={**headers, "X-Voice-Engine": "voicestudio"})
        except voices.VoiceError as e:
            headers["X-Voice-Fallback"] = str(e)[:200]
    try:
        audio = await voices.synthesize(text, voice, tone, rate, pitch)
    except voices.VoiceError as e:
        return _err(503, headers.get("X-Voice-Fallback") or str(e))
    return Response(audio, media_type="audio/mpeg", headers={**headers, "X-Voice-Engine": "edge"})


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
            f"Appearance: {c['appearance']} {profiles.looks(profiles.clean(c.get('profile')))}\n"
            f"Personality: {c['persona']}")
    messages = [{"role": "system", "content": portraits.PROMPT_INSTRUCTIONS},
                {"role": "user", "content": card}]
    try:
        text = (await _complete(s["user_id"], messages, 0.7, 300)).strip().strip('"')
        if len(text) > 40:
            return text
    except Exception:
        pass
    return portraits.template_prompt(c)


SCENE_CONTEXT_MESSAGES = 6  # earlier messages a scene picture reads for setting and lead-in
MAX_PICTURE_PEOPLE = 4


@router.post("/messages/{msg_id}/picture")
async def make_scene_picture(msg_id: int, request: Request, s: dict = Depends(_session)):
    """Picture of one roleplay moment. The message's own text sets the action; what's known about the
    characters in it (card, profile, memories) and the story so far keep their looks and the setting right.
    With "prompt" in the body that prompt is used as is; otherwise the chat model writes one."""
    m = await _row("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if not m or m["role"] not in ("user", "assistant"):
        return _err(404, "Message not found")
    if not portraits.enabled():
        return _err(503, "Pictures need a ComfyUI server. Set COMFYUI_URL in .env (see README).")
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()[:4000]
    if not prompt:
        prompt = await _scene_prompt(s, m)
    if mentions_minor(prompt, strict=True):
        return _err(400, "Pictures must show adults only.")
    try:
        png = await portraits.generate(api_routes.http_client, prompt,
                                       width=portraits.SCENE_WIDTH, height=portraits.SCENE_HEIGHT)
    except portraits.PortraitError as e:
        return _err(502, str(e))
    url = portraits.save(msg_id, png, kind="scene")
    await _exec("UPDATE messages SET image = ?, image_prompt = ? WHERE id = ?", (url, prompt, msg_id))
    return JSONResponse({"image": url, "prompt": prompt})


@router.delete("/messages/{msg_id}/picture")
async def delete_scene_picture(msg_id: int, s: dict = Depends(_session)):
    await _exec("UPDATE messages SET image = '', image_prompt = '' WHERE id = ?", (msg_id,))
    return JSONResponse({"ok": True})


def _mentions_name(name: str, text: str) -> bool:
    first = name.split()[0] if name.split() else name
    return any(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE) for n in {name, first} if len(n) > 1)


def _person_sheet(c: dict, mems: list[dict]) -> str:
    """Everything known about how a character looks, for a scene picture."""
    age = f"{c['age']}" if int(c["age"]) <= 100 else f"ageless, looks about 35 (really {c['age']})"
    prof = profiles.clean(c.get("profile"))
    lines = [f"{c['name']} ({c['gender']}, age {age})",
             f"  Appearance: {c['appearance'] or 'not described'}"]
    if profiles.looks(prof):
        lines.append(f"  Details: {profiles.looks(prof)}")
    for k in ("species", "powers"):
        if k in prof:
            lines.append(f"  {profiles.FIELDS[k]}: {prof[k]['value']}")
    if mems:
        lines.append("  Remembered from the story (may change how they look now): "
                     + " | ".join(x["content"] for x in mems))
    return "\n".join(lines)


async def _scene_prompt(s: dict, m: dict) -> str:
    """Ask the chat model for a Z-Image prompt of this moment, from the moment itself plus what came before."""
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (m["conversation_id"],))
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    current = _TIME_TAG.sub("", m["content"]).strip()
    earlier = await _rows("SELECT role, content FROM messages WHERE conversation_id = ? AND id < ? "
                          "AND role != 'mark' ORDER BY id DESC LIMIT ?",
                          (cv["id"], m["id"], SCENE_CONTEXT_MESSAGES))
    earlier.reverse()
    lead_in = "\n".join(f"{c['name'] if e['role'] == 'assistant' else s['username']}: "
                        f"{_TIME_TAG.sub('', e['content']).strip()[:600]}" for e in earlier)

    # Who is in it: the scene's character, plus anyone else from the library named in this moment or just before.
    people = [c]
    nearby = current + "\n" + "\n".join(e["content"] for e in earlier[-2:])
    for o in await _rows("SELECT * FROM characters WHERE id != ? ORDER BY id", (c["id"],)):
        if len(people) < MAX_PICTURE_PEOPLE and _mentions_name(o["name"], nearby):
            people.append(o)
    sheets = []
    for p in people:
        mems = await _rows("""SELECT content FROM memories WHERE character_id = ?
                              AND (storyline_id = ? OR storyline_id IS NULL) ORDER BY id DESC LIMIT 12""",
                           (p["id"], cv["storyline_id"]))
        sheets.append(_person_sheet(p, mems))

    when = ""
    if m["story_at"]:
        d = datetime.fromisoformat(m["story_at"])
        when = f"{_clock_text(m['story_at'])}, {_daypart(d)}, moon: {_moon(d)}"
    story = await _row("SELECT * FROM storylines WHERE id = ?", (cv["storyline_id"],))
    context = ""
    if story:
        context += f"Storyline: {story['title']}" + (f". {story['premise']}" if story["premise"] else "") + "\n"
    if cv["novel_id"]:
        novel = await _row("SELECT title, setting FROM novels WHERE id = ?", (cv["novel_id"],))
        if novel and novel["setting"]:
            context += f"Novel setting: {novel['setting']}\n"
    if cv["summary"]:
        context += f"Story so far: {cv['summary'][-1500:]}\n"
    who_wrote = c["name"] if m["role"] == "assistant" else s["username"]
    user_msg = (f"CHARACTERS (keep their looks exactly):\n" + "\n".join(sheets) + "\n"
                f"{s['username']} is the user's own character, and \"you\" in the text means {s['username']}; show "
                "them only as the text describes them, or frame the shot so they are seen from behind or partly out "
                "of frame. Words in `backticks` are private thoughts: they set the mood but are not visible.\n\n"
                + (f"BACKGROUND:\n{context}\n" if context else "")
                + (f"TIME: {when}\n\n" if when else "")
                + (f"JUST BEFORE (for where they are, what they wear, and lead-in only):\n{lead_in}\n\n" if lead_in else "")
                + f"CURRENT MOMENT, written by {who_wrote} (this is what the picture shows):\n{current[:2000]}")
    messages = [{"role": "system", "content": portraits.SCENE_INSTRUCTIONS},
                {"role": "user", "content": user_msg}]
    try:
        text = (await _complete(s["user_id"], messages, 0.6, 400)).strip().strip('"')
        if len(text) > 40:
            return text
    except Exception:
        pass
    # No chat model: a plain prompt from the moment and the main character's look.
    plain = re.sub(r'[*`"]', "", current)[:700]
    return (f"Cinematic photorealistic film still. {c['appearance']} {profiles.looks(profiles.clean(c.get('profile')))} "
            f"The moment: {plain} "
            + (f"Time of day: {_daypart(datetime.fromisoformat(m['story_at']))}. " if m["story_at"] else "")
            + "All people are adults. Tasteful, no explicit nudity. Natural skin texture, real light, 35mm lens, "
              "shallow depth of field. Not a drawing, not anime, not CGI.")


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
    known = [m["content"] for m in await _rows(
        "SELECT content FROM memories WHERE character_id = ? AND (storyline_id = ? OR storyline_id IS NULL) ORDER BY id",
        (c["id"], cv["storyline_id"]))]
    start = await _story_start(cv)
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
        await _exec("""INSERT INTO memories (character_id, storyline_id, content, source, conversation_id, message_id,
                       quote, said_at, story_time, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (c["id"], cv["storyline_id"], fact, "auto", conv_id, src["id"], quote, src["created_at"],
                     story_at.get(src["id"], ""), now))


@router.get("/characters/{cid}/memories")
async def list_memories(cid: int, storyline: int | None = None, s: dict = Depends(_session)):
    """A character's memories in one storyline (plus any that hold in every storyline)."""
    return JSONResponse(await _rows("SELECT id, content, source, storyline_id, conversation_id, message_id, quote, "
                                    "said_at, story_time, created_at FROM memories WHERE character_id = ? "
                                    "AND (? IS NULL OR storyline_id = ? OR storyline_id IS NULL) "
                                    "ORDER BY COALESCE(said_at, created_at) DESC, id DESC", (cid, storyline, storyline)))


@router.post("/characters/{cid}/memories")
async def add_memory(cid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    text = str(body.get("content") or "").strip()[:300]
    if not text:
        return _err(400, "Write the memory first.")
    if mentions_minor(text):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    sid = int(body["storyline_id"]) if body.get("storyline_id") and not body.get("everywhere") else None
    mid = await _exec("INSERT INTO memories (character_id, storyline_id, content, source, created_at) VALUES (?,?,?,?,?)",
                      (cid, sid, text, "user", time.time()))
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


# --- storylines ---

@router.get("/storylines")
async def storylines(s: dict = Depends(_session)):
    return JSONResponse(await _rows(
        """SELECT st.*, (SELECT COUNT(*) FROM conversations cv WHERE cv.storyline_id = st.id) AS scene_count
           FROM storylines st ORDER BY st.updated_at DESC"""))


@router.post("/storylines")
async def create_storyline(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    title, premise = str(body.get("title") or "").strip()[:120], str(body.get("premise") or "").strip()[:2000]
    if not title:
        return _err(400, "Give the storyline a title.")
    if mentions_minor(title + " " + premise):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    now = time.time()
    return JSONResponse({"id": await _exec("INSERT INTO storylines (title, premise, created_at, updated_at) "
                                           "VALUES (?,?,?,?)", (title, premise, now, now))})


@router.put("/storylines/{sid}")
async def update_storyline(sid: int, request: Request, s: dict = Depends(_session)):
    body = await request.json()
    title, premise = str(body.get("title") or "").strip()[:120], str(body.get("premise") or "").strip()[:2000]
    if not title:
        return _err(400, "Give the storyline a title.")
    if mentions_minor(title + " " + premise):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    await _exec("UPDATE storylines SET title = ?, premise = ?, updated_at = ? WHERE id = ?",
                (title, premise, time.time(), sid))
    return JSONResponse({"ok": True})


@router.delete("/storylines/{sid}")
async def delete_storyline(sid: int, s: dict = Depends(_session)):
    """Delete a storyline with its scenes and the memories learned in it."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE conversation_id IN "
                         "(SELECT id FROM conversations WHERE storyline_id = ?)", (sid,))
        await db.execute("DELETE FROM conversations WHERE storyline_id = ?", (sid,))
        await db.execute("DELETE FROM memories WHERE storyline_id = ?", (sid,))
        await db.execute("DELETE FROM storylines WHERE id = ?", (sid,))
        await db.commit()
    return JSONResponse({"ok": True})


# --- roleplay chats ---

@router.get("/conversations")
async def conversations(s: dict = Depends(_session)):
    return JSONResponse(await _rows(
        """SELECT cv.id, cv.character_id, cv.novel_id, cv.storyline_id, cv.updated_at, cv.location, c.name, c.avatar, c.color,
             n.title AS novel_title, st.title AS storyline_title,
             (SELECT content FROM messages m WHERE m.conversation_id = cv.id ORDER BY m.id DESC LIMIT 1) AS last
           FROM conversations cv JOIN characters c ON c.id = cv.character_id
           LEFT JOIN novels n ON n.id = cv.novel_id
           LEFT JOIN storylines st ON st.id = cv.storyline_id
           ORDER BY cv.updated_at DESC LIMIT 200"""))


@router.post("/conversations")
async def start_conversation(request: Request, s: dict = Depends(_session)):
    body = await request.json()
    c = await _row("SELECT * FROM characters WHERE id = ?", (int(body.get("character_id") or 0),))
    if not c:
        return _err(404, "Character not found")
    novel_id = body.get("novel_id") or None
    storyline_id = int(body.get("storyline_id") or 0) or None
    if storyline_id and not await _row("SELECT id FROM storylines WHERE id = ?", (storyline_id,)):
        return _err(404, "Storyline not found")
    if not body.get("new"):
        existing = await _row("SELECT id FROM conversations WHERE character_id = ? AND novel_id IS ? "
                              "AND (? IS NULL OR storyline_id = ?) ORDER BY updated_at DESC LIMIT 1",
                              (c["id"], novel_id, storyline_id, storyline_id))
        if existing:
            return JSONResponse({"id": existing["id"]})
    now = time.time()
    if not storyline_id:
        if body.get("storyline_from"):  # "New scene" keeps the scene it came from in the same storyline
            src = await _row("SELECT storyline_id FROM conversations WHERE id = ?", (int(body["storyline_from"]),))
            storyline_id = src["storyline_id"] if src else None
        if not storyline_id:
            storyline_id = await _exec("INSERT INTO storylines (title, created_at, updated_at) VALUES (?,?,?)",
                                       (f"{c['name']}'s story", now, now))
    try:
        if body.get("story_start"):
            start = datetime.fromisoformat(str(body["story_start"]))
        else:  # pick up where the storyline left off
            start = await _storyline_clock(storyline_id) or datetime.now()
    except ValueError:
        return _err(400, "That story start time isn't a valid date and time.")
    location = " ".join(str(body.get("location") or "").split())[:300]
    situation = str(body.get("situation") or "").strip()[:1500]
    first_line = str(body.get("opening") or "").strip()[:2000]  # the scene's first message, written by the user
    user_first = body.get("first") == "you" and bool(first_line)
    if mentions_minor(location + " " + situation + " " + first_line, strict=True):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    cv = await _exec("INSERT INTO conversations (character_id, novel_id, storyline_id, location, situation, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (c["id"], novel_id, storyline_id, location, situation, now, now))
    await _exec("UPDATE storylines SET updated_at = ? WHERE id = ?", (now, storyline_id))
    if location or situation:
        mark = f"Starts at: {location}" if location else "Scenario: " + (situation[:80] + ("…" if len(situation) > 80 else ""))
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                    (cv, "mark", mark, _iso(start), now))
    if user_first:  # the user opens the scene; the web app asks for the character's reply next
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                    (cv, "user", first_line, _iso(start), now))
        return JSONResponse({"id": cv, "awaiting_reply": True})
    opening = first_line or c["greeting"]
    if not first_line and (location or situation):
        opening = await _opening(s, c, storyline_id, location, situation, start) or opening
    await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                (cv, "assistant", opening, _iso(start), now))
    return JSONResponse({"id": cv})


@router.get("/places")
async def list_places(character_id: int = 0, s: dict = Depends(_session)):
    c = await _row("SELECT * FROM characters WHERE id = ?", (character_id,)) if character_id else None
    return JSONResponse({"groups": places.catalog(c)})


@router.get("/option-image")
async def option_image(set: str, key: str, g: str = "", s: dict = Depends(_session)):
    """Realistic photo for a choice tile in the character creator or the place picker."""
    key = " ".join(key.split())[:200]
    if set not in option_images.TEMPLATES or not key:
        return _err(404, "No picture for that.")
    if mentions_minor(key, strict=True):
        return _err(400, "Pictures must show adults only.")
    try:
        png = await option_images.get(api_routes.http_client, set, key, g if g in ("male", "female", "non-binary") else "")
    except option_images.OptionImageError as e:
        return _err(503, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=2592000"})


async def _opening(s: dict, c: dict, storyline_id: int, location: str, situation: str, start: datetime) -> str:
    """The character's first message, set at the chosen place. Empty if the model can't write one."""
    story = await _row("SELECT * FROM storylines WHERE id = ?", (storyline_id,))
    system = (f"You are {c['name']}, a {c['age']}-year-old {c['gender']} character in an interactive dark romance "
              f"role-play with {s['username']}. " + LIFELIKE_RULES + "\n\n"
              f"Personality and background: {c['persona']}\nAppearance: {c['appearance']}\n"
              + profiles.for_prompt(profiles.clean(c["profile"]), c["name"], s["username"])
              + (f"\nStoryline: {story['title']}" + (f". {story['premise']}" if story["premise"] else "") + "\n"
                 if story else "")
              + f"\nYour usual opening line, for your voice only: {c['greeting']}\n\n" + SAFETY_RULES)
    ask = (f"Write {c['name']}'s opening message for a new scene. Place: {location or 'your choice'}. "
           f"Time: {_clock_text(_iso(start))}, {_daypart(start)}. "
           + (f"Scenario: {situation} " if situation else "")
           + f"Set the scene through {c['name']}'s senses and actions, then give {s['username']} something to "
           "answer. 1 to 2 paragraphs. Never speak or act for the user. Write spoken words in \"double quotes\", "
           "actions in *asterisks* and private thoughts in `backticks`. No time tags, no preamble.")
    try:
        text = (await _complete(s["user_id"], [{"role": "system", "content": system},
                                               {"role": "user", "content": ask}], 0.9, 450)).strip()
    except Exception:
        return ""
    text = _TIME_TAG.sub("", text).strip()
    return text if len(text) > 20 and not mentions_minor(text) else ""


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: int, s: dict = Depends(_session)):
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    novel = await _row("SELECT id, title FROM novels WHERE id = ?", (cv["novel_id"],)) if cv["novel_id"] else None
    msgs = await _rows("SELECT id, role, content, story_at, image, image_prompt, created_at FROM messages "
                       "WHERE conversation_id = ? ORDER BY id", (conv_id,))
    upto = next((m["created_at"] for m in msgs if m["id"] == cv["summarized_upto"]), None)
    return JSONResponse({"id": conv_id, "character": c, "novel": novel, "messages": msgs, "summary": cv["summary"],
                         "summary_upto": upto, "clock": _iso(await _scene_clock(cv)),
                         "location": cv["location"] or "", "situation": cv["situation"] or "",
                         "start": await _story_start(cv),
                         "storyline": await _row("SELECT * FROM storylines WHERE id = ?", (cv["storyline_id"],)),
                         "time_rules": json.loads(cv["time_rules"]) if cv["time_rules"] else []})



@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: int, s: dict = Depends(_session)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        await db.commit()
    return JSONResponse({"ok": True})


@router.put("/messages/{msg_id}")
async def edit_message(msg_id: int, request: Request, s: dict = Depends(_session)):
    """Rewrite one of your messages or one of the character's replies."""
    body = await request.json()
    text = str(body.get("content") or "").strip()[:6000]
    m = await _row("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if not m or m["role"] not in ("user", "assistant"):
        return _err(404, "Message not found")
    if not text:
        return _err(400, "A message can't be empty. Use Rewind to remove it.")
    if mentions_minor(text):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    await _exec("UPDATE messages SET content = ? WHERE id = ?", (text, msg_id))
    return JSONResponse({"ok": True, "content": text})


@router.post("/conversations/{conv_id}/suggest")
async def suggest_reply(conv_id: int, request: Request, s: dict = Depends(_session)):
    """Write (or polish) the user's next message, in their voice, for the magic-wand button."""
    body = await request.json()
    draft = str(body.get("draft") or "").strip()[:2000]
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    if draft and mentions_minor(draft):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    recent = await _rows("SELECT role, content FROM messages WHERE conversation_id = ? AND role != 'mark' "
                         "ORDER BY id DESC LIMIT 8", (conv_id,))
    lines = "\n\n".join(f"{c['name'] if m['role'] == 'assistant' else s['username']}: "
                         + _TIME_TAG.sub("", m["content"]).strip() for m in reversed(recent))
    system = (f"You help {s['username']} write their next message in a private dark romance role-play with "
              f"{c['name']}. Write only {s['username']}'s own message, in first person, as {s['username']}: never "
              f"speak or act for {c['name']}. Spoken words in \"double quotes\", actions in *asterisks*, private "
              "thoughts in `backticks`. 1 short paragraph. No preamble, no name label, no time tags.\n\n"
              + SAFETY_RULES)
    ask = (("Scene so far:\n" + lines + "\n\n") if lines else "") + (
        f"{s['username']}'s rough draft: {draft}\nRewrite it as a vivid message that keeps exactly what they meant."
        if draft else f"Write a message {s['username']} could send next that moves the scene forward.")
    try:
        text = (await _complete(s["user_id"], [{"role": "system", "content": system},
                                               {"role": "user", "content": ask}], 0.9, 220)).strip()
    except Exception:
        return _err(502, "Couldn't write a suggestion right now. Try again.")
    text = re.sub(rf"^\s*{re.escape(s['username'])}\s*:\s*", "", _TIME_TAG.sub("", text)).strip()
    if not text or mentions_minor(text):
        return _err(502, "Couldn't write a suggestion right now. Try again.")
    return JSONResponse({"text": text})


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
        actions = _clean_actions(body.get("actions"))
    except ValueError as e:
        return _err(400, str(e) if "“" in str(e) else "Minutes must be numbers.")
    if pace not in PACES:
        return _err(400, "Pick a pace of 0.5x, 1.0x or 1.5x.")
    if not actions:
        return _err(400, "Keep at least one action.")
    await _exec("INSERT INTO studio_settings (key, value) VALUES ('time_rules', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps({"pace": pace, "actions": actions}),))
    return JSONResponse({"ok": True})


@router.put("/conversations/{conv_id}/time-rules")
async def save_scene_time_rules(conv_id: int, request: Request, s: dict = Depends(_session)):
    """Time rules for one scene: they add to the global table, and replace a global rule with the same name."""
    body = await request.json()
    try:
        actions = _clean_actions(body.get("actions"))
    except ValueError as e:
        return _err(400, str(e))
    await _exec("UPDATE conversations SET time_rules = ? WHERE id = ?", (json.dumps(actions) if actions else "", conv_id))
    return JSONResponse({"ok": True, "actions": actions})


@router.put("/messages/{msg_id}/time")
async def override_message_time(msg_id: int, request: Request, s: dict = Depends(_session)):
    """Change when a reply ends ("03:15 AM") or how long it took ("45 min"). Every later
    message in the scene moves by the same amount, so the timeline stays consistent."""
    body = await request.json()
    m = await _row("SELECT * FROM messages WHERE id = ?", (msg_id,))
    if not m or not m["story_at"]:
        return _err(404, "Message not found")
    prev = await _row("SELECT story_at FROM messages WHERE conversation_id = ? AND id < ? AND story_at != '' "
                      "ORDER BY id DESC LIMIT 1", (m["conversation_id"], msg_id))
    base = datetime.fromisoformat(prev["story_at"] if prev else m["story_at"])
    new = _time_value(str(body.get("value") or ""), base)
    if new is None:
        return _err(400, "Write a time like 03:15 AM or a length like 45 min or 2 hours.")
    delta = new - datetime.fromisoformat(m["story_at"])
    later = await _rows("SELECT id, story_at FROM messages WHERE conversation_id = ? AND id >= ? AND story_at != ''",
                        (m["conversation_id"], msg_id))
    async with aiosqlite.connect(DB_PATH) as db:
        for r in later:
            await db.execute("UPDATE messages SET story_at = ? WHERE id = ?",
                             (_iso(datetime.fromisoformat(r["story_at"]) + delta), r["id"]))
        await db.commit()
    return JSONResponse({"ok": True, "story_at": _iso(new), "shifted": len(later)})


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
    more = bool(body.get("continue")) and not text  # carry on the character's last reply
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    now = time.time()
    clock = await _scene_clock(cv)

    override = None  # minutes the reply must cover, from a [Time: ...] tag in the message
    if text:  # an empty message means "regenerate the reply to the last message"
        tag = _OVERRIDE.search(text)
        if tag:
            until = _time_value(tag.group(1), clock)
            if until is None:
                return _err(400, "Write the time like [Time: 45 min], [Time: 2 hours] or [Time: 03:15 AM].")
            override = (until - clock).total_seconds() / 60
            text = re.sub(r"[ \t]{2,}", " ", _OVERRIDE.sub("", text)).strip()
            if not text:
                return _err(400, "Write what happens too, with the [Time: ...] tag.")
        if mentions_minor(text):
            return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, time_override, created_at) "
                    "VALUES (?,?,?,?,?,?)", (conv_id, "user", text, _iso(clock), override, now))
        if is_crisis(text):
            await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) "
                        "VALUES (?,?,?,?,?)", (conv_id, "assistant", CRISIS_RESPONSE, _iso(clock), now))
            return JSONResponse({"crisis": True, "content": CRISIS_RESPONSE})
    await _exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))

    system = (
        f"You are {c['name']}, a {c['age']}-year-old {c['gender']} character in an interactive dark romance "
        f"role-play with {s['username']}. Stay in character. Write only {c['name']}'s replies, 1 to 3 paragraphs, and never speak or act for the user. "
        "Format every reply the same way: spoken words in \"double quotes\", actions and body language in "
        f"*asterisks*, and {c['name']}'s private inner thoughts in `backticks`, like: *He leans closer.* "
        "`She has no idea.` \"You should go.\" " + LIFELIKE_RULES + " Dark themes, tension "
        "and explicit adult content between consenting adult characters are allowed in this private story.\n\n"
        f"Personality and background: {c['persona']}\nAppearance: {c['appearance']}\n"
    ) + profiles.for_prompt(profiles.clean(c["profile"]), c["name"], s["username"])
    if cv["novel_id"]:
        novel, cast = await _novel_with_cast(cv["novel_id"])
        if novel:
            system += "\nThis role-play is a scene for the user's novel. Story bible:\n" + _bible(novel, cast) + "\n"
    story = await _row("SELECT * FROM storylines WHERE id = ?", (cv["storyline_id"],))
    if story:
        system += f"\nStoryline: {story['title']}" + (f". {story['premise']}" if story["premise"] else "") + "\n"
    if cv["location"] or cv["situation"]:
        system += ("\nThis scene began " + (f"at: {cv['location']}." if cv["location"] else "") +
                   (f" Scenario: {cv['situation']}" if cv["situation"] else "") +
                   " Keep the place's details consistent; people can move elsewhere as the story goes.\n")
    # User-written memories always go in; the newest automatic ones fill the rest.
    mems = await _rows("""SELECT * FROM (SELECT content, said_at, story_time, created_at, source, id FROM memories
                          WHERE character_id = ? AND (storyline_id = ? OR storyline_id IS NULL)
                          ORDER BY source = 'user' DESC, id DESC LIMIT ?)
                          ORDER BY COALESCE(said_at, created_at), id""",
                       (c["id"], cv["storyline_id"], MAX_MEMORIES_IN_PROMPT))
    if mems:
        system += (f"\nThings {c['name']} remembers about {s['username']}, oldest first. These are facts:\n"
                   + "\n".join(f"- [{_stamp(m['said_at'] or m['created_at'])}"
                                + (f" | story time: {m['story_time']}" if m["story_time"] else "")
                                + f"] {m['content']}" for m in mems) + "\n")
    if cv["summary"]:
        system += f"\nThis scene so far (earlier messages, summarized in order):\n{cv['summary']}\n"

    history = await _rows("SELECT id, role, content, story_at, time_override, created_at FROM messages "
                          "WHERE conversation_id = ? ORDER BY id", (conv_id,))
    window = _history_window(history)
    first_in_window = history[len(history) - len(window)]["id"] if window else 0
    recalled = await _recall(cv, first_in_window, text or (history[-1]["content"] if history else ""),
                             s["username"])
    if recalled:
        system += ("\nExact lines from earlier, word for word, with when they were said:\n"
                   + "\n".join(f"[{_stamp(m['created_at'])}"
                                + (f" | story time {_clock_text(m['story_at'])}" if m["story_at"] else "")
                                + f"] {m['who']}: {m['content']}" for m in recalled) + "\n")
    marks = [m for m in history if m["role"] == "mark"]
    start = await _story_start(cv) or _iso(clock)
    tset = await _time_settings()
    actions = _merge_actions(tset["actions"], json.loads(cv["time_rules"]) if cv["time_rules"] else [])
    if not text and history and history[-1]["role"] == "user":  # regenerate keeps the override
        override = history[-1]["time_override"]
    system += (f"\nCurrent in-universe time: {_clock_text(_iso(clock), start)}, {_daypart(clock)}, "
               f"moon: {_moon(clock).lower()}"
               + (f" (latest story mark: {marks[-1]['content']})" if marks else "") + "\n\n"
               + _time_rules(actions) + "\n")
    if override is not None:
        end = clock + timedelta(minutes=override)
        system += (f"\nThe user has set this exchange to take exactly {_span(override)}, ending at "
                   f"{end.strftime('%I:%M %p')} (Day {_day(_iso(end), start)}). Write the reply to cover that "
                   f"span, with a transition if hours pass, and end with [Time passed: {_span(override)}].\n")
    system += (f"\nContinuity: keep every detail consistent with the memories, summary and exact lines above "
               "(names, dates, places, promises, who did what). Never invent past events that aren't there; if "
               f"{c['name']} isn't sure, they stay vague instead of making it up.\n\n" + SAFETY_RULES)

    messages = [{"role": "system", "content": system}] + _chat_turns(window, start)
    last = history[-1] if history else None
    if more:
        if not last or last["role"] != "assistant":
            return _err(400, "There's no reply to continue.")
        messages.append({"role": "user", "content": f"[Continue {c['name']}'s last reply right where it stopped, "
                         "with 1 to 2 more paragraphs. Don't repeat anything already written.]"})

    async def save(reply):
        reply, minutes = _elapsed(reply, float(tset["pace"]))
        if override is not None:
            minutes = override
        if more:
            base = datetime.fromisoformat(last["story_at"]) if last["story_at"] else clock
            await _exec("UPDATE messages SET content = ?, story_at = ? WHERE id = ?",
                        (last["content"].rstrip() + "\n\n" + reply.strip(), _iso(base + timedelta(minutes=minutes)),
                         last["id"]))
            return
        await _exec("INSERT INTO messages (conversation_id, role, content, story_at, created_at) VALUES (?,?,?,?,?)",
                    (conv_id, "assistant", reply, _iso(clock + timedelta(minutes=minutes)), time.time()))
        asyncio.create_task(compact_conversation(conv_id, s["user_id"], s["username"]))

    model = body.get("model_id") or DEFAULT_MODEL
    return await _stream(request, s, model, messages, 0.9, 450, save)


# --- roleplay to manuscript ---
# A scene (or a whole storyline) is rewritten as novel chapters, one part at a time, in the background.

MANUSCRIPT_PART_CHARS = 9000   # transcript per chapter
MANUSCRIPT_MAX_PARTS = 40
_manuscripts: dict[int, dict] = {}  # novel id -> {"done", "total", "error", "running"}


def _manuscript_parts(msgs: list[dict], name: str, you: str, start: str) -> list[str]:
    parts, cur = [], ""
    for m in msgs:
        if m["role"] == "mark":
            line = f"[Scene note: {m['content']}" + (f" | {_clock_text(m['story_at'], start)}]" if m.get("story_at") else "]")
        else:
            line = f"{name if m['role'] == 'assistant' else you}: " + _TIME_TAG.sub("", m["content"]).strip()
        if cur and len(cur) + len(line) > MANUSCRIPT_PART_CHARS:
            parts.append(cur)
            cur = ""
        cur += line + "\n\n"
    if cur.strip():
        parts.append(cur)
    return parts[:MANUSCRIPT_MAX_PARTS]


async def _write_manuscript(nid: int, chapter_ids: list[int], parts: list[str], user_id: int, you: str, base: int):
    job = _manuscripts[nid]
    novel, cast = await _novel_with_cast(nid)
    before = await _row("SELECT content FROM chapters WHERE novel_id = ? AND position = ?", (nid, base)) if base else None
    previous = (before["content"] if before else "")[-1200:]
    try:
        for i, (cid, part) in enumerate(zip(chapter_ids, parts)):
            system = ("You are a novelist turning a private role-play transcript into a chapter of a dark romance "
                      f"manuscript. Story bible:\n{_bible(novel, cast)}\n\n"
                      f"{you} is the role-play's other lead; write them as a full character named {you}. "
                      f"Point of view and tense: {novel['pov']}. Keep every event, choice and important line of "
                      "dialogue from the transcript, in order; polish the words, add setting, body language and "
                      "interiority, and smooth the jumps between turns into continuous prose. Never add events that "
                      "aren't there. Inner thoughts in the transcript (in backticks) become interior narration. "
                      "Use normal book formatting: dialogue in double quotes, no asterisks, no backticks, no time "
                      "tags, no speaker labels. " + LIFELIKE_RULES + "\n\n" + SAFETY_RULES)
            ask = ((f"The previous chapter ended like this:\n{previous}\n\n" if previous else "This is the opening chapter.\n\n")
                   + f"Transcript for chapter {base + i + 1}:\n{part}\n"
                   "Reply with the chapter title on the first line as 'Title: ...', then the chapter.")
            text = (await _complete(user_id, [{"role": "system", "content": system},
                                              {"role": "user", "content": ask}], 0.8, 3000, novel["model_id"])).strip()
            text = _TIME_TAG.sub("", text)
            title, body = f"Chapter {base + i + 1}", text
            first, _, rest = text.partition("\n")
            if first.lower().lstrip("#* ").startswith("title:"):
                title = f"Chapter {base + i + 1}: " + first.split(":", 1)[1].strip(" *#\"")[:100]
                body = rest.strip()
            if mentions_minor(body):
                body = "[This part was left out: the draft broke Dreamnet's adults-only rule. Rewrite it by hand.]"
            await _exec("UPDATE chapters SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                        (title, body, time.time(), cid))
            previous = body[-1200:]
            job["done"] = i + 1
    except Exception as e:  # keep what's written; the rest stays as empty chapters
        job["error"] = f"Stopped after {job['done']} of {job['total']} chapters: {e}"[:300]
    finally:
        job["running"] = False
        await _exec("UPDATE novels SET updated_at = ? WHERE id = ?", (time.time(), nid))


@router.post("/conversations/{conv_id}/manuscript")
async def make_manuscript(conv_id: int, request: Request, s: dict = Depends(_session)):
    """Turn this scene, or its whole storyline, into a manuscript: a new novel, or new chapters in one."""
    body = await request.json()
    cv = await _row("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    if not cv:
        return _err(404, "Chat not found")
    c = await _row("SELECT * FROM characters WHERE id = ?", (cv["character_id"],))
    story = await _row("SELECT * FROM storylines WHERE id = ?", (cv["storyline_id"],)) if cv["storyline_id"] else None
    whole = body.get("scope") == "storyline" and story
    convs = (await _rows("SELECT * FROM conversations WHERE storyline_id = ? ORDER BY id", (story["id"],))) if whole else [cv]
    you = " ".join(str(body.get("you") or s["username"]).split())[:60] or s["username"]
    start = await _story_start(cv) or ""
    msgs = []
    for x in convs:
        msgs += await _rows("SELECT role, content, story_at FROM messages WHERE conversation_id = ? ORDER BY id", (x["id"],))
    if not any(m["role"] == "assistant" for m in msgs):
        return _err(400, "There's nothing to write yet. Play the scene first.")
    parts = _manuscript_parts(msgs, c["name"], you, start)
    title = " ".join(str(body.get("title") or "").split())[:120]
    pov = str(body.get("pov") or "").strip()[:80] or NOVEL_DEFAULTS["pov"]
    if mentions_minor(title + " " + you):
        return _err(400, "Dreamnet doesn't allow content involving anyone under 18.")
    now = time.time()
    nid = int(body.get("novel_id") or 0)
    if nid:
        if not await _row("SELECT id FROM novels WHERE id = ?", (nid,)):
            return _err(404, "Novel not found")
        if any(j.get("running") for k, j in _manuscripts.items() if k == nid):
            return _err(409, "That novel is already being written. Wait for it to finish.")
        # drop the empty starter chapter a new novel comes with
        await _exec("DELETE FROM chapters WHERE novel_id = ? AND content = '' AND position = 1 AND "
                    "(SELECT COUNT(*) FROM chapters WHERE novel_id = ?) = 1", (nid, nid))
    else:
        premise = " ".join(x for x in [story["premise"] if story else "", cv["situation"] or "",
                                       f"It begins at {cv['location']}." if cv["location"] else ""] if x)
        nid = await _exec(f"INSERT INTO novels ({', '.join(NOVEL_FIELDS)}, created_at, updated_at) "
                          f"VALUES ({', '.join('?' * len(NOVEL_FIELDS))}, ?, ?)",
                          (title or (story["title"] if story else f"{c['name']} and {you}"), premise[:6000], "",
                           cv["location"] or "", pov, NOVEL_DEFAULTS["tone"], NOVEL_DEFAULTS["heat"],
                           f"Adapted from a role-play. {you} is the other lead.", None, now, now))
        await _exec("INSERT OR REPLACE INTO novel_cast (novel_id, character_id, role) VALUES (?,?,?)",
                    (nid, c["id"], "lead"))
    base = (await _row("SELECT COALESCE(MAX(position), 0) AS p FROM chapters WHERE novel_id = ?", (nid,)))["p"]
    ids = []
    for i in range(len(parts)):
        ids.append(await _exec("INSERT INTO chapters (novel_id, position, title, content, updated_at) VALUES (?,?,?,?,?)",
                               (nid, base + i + 1, f"Chapter {base + i + 1} (writing…)", "", now)))
    if not whole:
        await _exec("UPDATE conversations SET novel_id = COALESCE(novel_id, ?) WHERE id = ?", (nid, conv_id))
    _manuscripts[nid] = {"done": 0, "total": len(parts), "error": "", "running": True}
    asyncio.create_task(_write_manuscript(nid, ids, parts, s["user_id"], you, base))
    return JSONResponse({"novel_id": nid, "chapters": len(parts)})


@router.get("/novels/{nid}/manuscript")
async def manuscript_status(nid: int, s: dict = Depends(_session)):
    job = _manuscripts.get(nid)
    return JSONResponse(job or {"done": 0, "total": 0, "error": "", "running": False})


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
