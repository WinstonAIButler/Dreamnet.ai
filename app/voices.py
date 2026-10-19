"""
Dreamnet - read-aloud voices
Two engines:
- Microsoft Edge's online Read Aloud voices (the edge-tts package): English
  neural voices grouped by country. Needs an internet connection.
- VoiceStudio (github.com/debpalash/VoiceStudio), running on your own computer:
  local, offline voices designed from the character (gender, age, pitch,
  accent), or any voice you designed or cloned in VoiceStudio. Dreamnet talks to
  its OpenAI-compatible speech API.
When neither answers, the web app falls back to the English voices on your device.
"""
import hashlib
import os
import re
from pathlib import Path

import httpx

try:
    import edge_tts
except ImportError:  # optional dependency
    edge_tts = None

TTS_CACHE = Path(os.getenv("TTS_CACHE", "tts_cache"))
VOICESTUDIO_URL = os.getenv("VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
VOICESTUDIO_API_KEY = os.getenv("VOICESTUDIO_API_KEY", "")
VOICESTUDIO_TIMEOUT = float(os.getenv("VOICESTUDIO_TIMEOUT", "300"))
ENGINES = ("edge", "voicestudio")

# English-speaking voices only, by country. Ids are Edge voice names.
REGIONS = [
    ("en-US", "United States"), ("en-GB", "United Kingdom"), ("en-AU", "Australia"), ("en-CA", "Canada"),
    ("en-IE", "Ireland"), ("en-NZ", "New Zealand"), ("en-ZA", "South Africa"), ("en-IN", "India"),
    ("en-SG", "Singapore"), ("en-PH", "Philippines"), ("en-NG", "Nigeria"), ("en-KE", "Kenya"),
    ("en-TZ", "Tanzania"), ("en-HK", "Hong Kong"),
]
_VOICES = {
    "en-US": [("Andrew", "male"), ("Brian", "male"), ("Christopher", "male"), ("Eric", "male"), ("Guy", "male"),
              ("Roger", "male"), ("Steffan", "male"),
              ("Ava", "female"), ("Aria", "female"), ("Emma", "female"), ("Jenny", "female"), ("Michelle", "female")],
    "en-GB": [("Ryan", "male"), ("Thomas", "male"), ("Sonia", "female"), ("Libby", "female"), ("Maisie", "female")],
    "en-AU": [("William", "male"), ("Natasha", "female")],
    "en-CA": [("Liam", "male"), ("Clara", "female")],
    "en-IE": [("Connor", "male"), ("Emily", "female")],
    "en-NZ": [("Mitchell", "male"), ("Molly", "female")],
    "en-ZA": [("Luke", "male"), ("Leah", "female")],
    "en-IN": [("Prabhat", "male"), ("Neerja", "female")],
    "en-SG": [("Wayne", "male"), ("Luna", "female")],
    "en-PH": [("James", "male"), ("Rosa", "female")],
    "en-NG": [("Abeo", "male"), ("Ezinne", "female")],
    "en-KE": [("Chilemba", "male"), ("Asilia", "female")],
    "en-TZ": [("Elimu", "male"), ("Imani", "female")],
    "en-HK": [("Sam", "male"), ("Yan", "female")],
}
VOICES = [{"id": f"{loc}-{name}Neural", "name": name, "gender": g, "region": loc}
          for loc, _ in REGIONS for name, g in _VOICES[loc]]
VOICE_IDS = {v["id"] for v in VOICES}

# Each voice can be pitched deeper or brighter, which gives every country
# several distinct-sounding options per gender.
TONES = {"deep": "-14Hz", "low": "-7Hz", "natural": "+0Hz", "bright": "+8Hz"}
DEFAULT_VOICE = {"male": "en-US-AndrewNeural", "female": "en-US-AvaNeural", "non-binary": "en-GB-SoniaNeural"}

# VoiceStudio's voice-design tags (omnivoice/utils/voice_design.py). It has five
# English accents that fit our countries; the rest use the closest one.
VS_ACCENT = {"en-US": "american accent", "en-CA": "canadian accent", "en-AU": "australian accent",
             "en-NZ": "australian accent", "en-IN": "indian accent", "en-PH": "american accent"}
VS_PITCH = {"deep": "very low pitch", "low": "low pitch", "natural": "moderate pitch", "bright": "high pitch"}


def vs_design(gender: str, age: int, tone: str, region: str, whisper: bool = False) -> str:
    """VoiceStudio design instruct for a character, e.g. "male, middle-aged, low pitch, british accent"."""
    tags = [gender] if gender in ("male", "female") else []
    # Adults only. Ageless characters (over 100) sound the age they look.
    tags.append("young adult" if age < 35 else "middle-aged" if age < 60 or age > 100 else "elderly")
    tags.append(VS_PITCH.get(tone, "moderate pitch"))
    if whisper:
        tags.append("whisper")
    tags.append(VS_ACCENT.get(region, "british accent"))
    return ", ".join(tags)


def vs_seed(key) -> int:
    """A fixed seed per character, so a designed voice keeps the same identity line after line."""
    return int(hashlib.sha256(f"dreamnet|{key}".encode()).hexdigest()[:8], 16)


def _vs_headers() -> dict:
    return {"Authorization": f"Bearer {VOICESTUDIO_API_KEY}"} if VOICESTUDIO_API_KEY else {}


async def voicestudio_status(client: httpx.AsyncClient) -> dict:
    """Is VoiceStudio running, and which saved voices (designed or cloned there) does it have?"""
    try:
        r = await client.get(f"{VOICESTUDIO_URL}/v1/audio/voices", headers=_vs_headers(), timeout=3)
        r.raise_for_status()
        profiles = [{"id": v.get("voice_id") or v.get("id"), "name": v.get("name") or v.get("voice_id")}
                    for v in r.json().get("voices", []) if v.get("type") == "profile"]
        return {"online": True, "url": VOICESTUDIO_URL, "profiles": profiles}
    except Exception:
        return {"online": False, "url": VOICESTUDIO_URL, "profiles": []}


# What to read aloud from a roleplay reply.
READ_MODES = ("all", "words_thoughts", "words")


class VoiceError(Exception):
    pass


def enabled() -> bool:
    return edge_tts is not None


def segments(text: str, mode: str = "all") -> list[tuple[str, str]]:
    """Roleplay markup as (kind, plain text) runs to read: speech, narration, action or thought."""
    text = re.sub(r"[*_]*\[\s*(?:time passed|elapsed time|current time)\s*:[^\]]*\][*_]*", "", text, flags=re.I)
    parts = re.split(r'(`[^`\n]+`|\(\([^\n]+?\)\)|\*[^*\n]+\*|"[^"\n]+"|“[^”\n]+”)', text)
    out = []
    for i, p in enumerate(parts):
        if not p.strip():
            continue
        kind = "narration" if i % 2 == 0 else {"`": "thought", "(": "thought", "*": "action"}.get(p[0], "speech")
        if kind == "thought":
            p = p.strip("`()")
        elif kind == "action":
            p = p.strip("*")
        if mode == "words" and kind != "speech" or mode == "words_thoughts" and kind not in ("speech", "thought"):
            continue
        out.append((kind, re.sub(r"\s+", " ", p).strip()))
    return out


def speakable(text: str, mode: str = "all") -> str:
    """Turn roleplay markup into plain text to read: "words", *actions*, `thoughts`."""
    return " ".join(t for _, t in segments(text, mode))[:5000]


async def synthesize(text: str, voice: str, tone: str = "natural", rate: int = 0) -> bytes:
    """MP3 audio for text in the given voice, cached on disk."""
    if not enabled():
        raise VoiceError("Read-aloud voices need the edge-tts package. Run the launcher again to install it.")
    if voice not in VOICE_IDS:
        raise VoiceError("Pick one of the listed voices.")
    pitch = TONES.get(tone, "+0Hz")
    rate = max(-50, min(50, int(rate)))
    key = hashlib.sha256(f"{voice}|{pitch}|{rate}|{text}".encode()).hexdigest()[:32]
    path = TTS_CACHE / f"{key}.mp3"
    if path.exists():
        return path.read_bytes()
    try:
        com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", pitch=pitch)
        audio = bytearray()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
    except Exception as e:  # network down, service changed
        raise VoiceError(f"The online voice service didn't answer ({type(e).__name__}).") from e
    if not audio:
        raise VoiceError("The online voice service returned no audio.")
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(audio))
    return bytes(audio)


async def synthesize_voicestudio(client: httpx.AsyncClient, text: str, mode: str, *, profile: str = "",
                                 gender: str = "female", age: int = 30, tone: str = "natural",
                                 region: str = "en-US", rate: int = 0, seed_key="") -> bytes:
    """MP3 from VoiceStudio, cached on disk. A saved VoiceStudio voice is used as is; otherwise the voice is
    designed from the character. Designed voices whisper the character's private thoughts."""
    runs: list[list] = []
    for kind, part in segments(text, mode):
        whisper = kind == "thought" and not profile
        if runs and runs[-1][0] == whisper:
            runs[-1][1] += " " + part
        else:
            runs.append([whisper, part])
    if not runs:
        raise VoiceError("Nothing to read in that message.")
    speed = round(1 + max(-50, min(50, int(rate))) / 100, 2)
    seed = vs_seed(seed_key)
    bodies = []
    for whisper, part in runs:
        body = {"model": "tts-1", "input": part[:4000], "response_format": "mp3", "speed": speed,
                "language": "en", "seed": seed}
        if profile:
            body["voice"] = profile
        else:
            body["instruct"] = vs_design(gender, int(age), tone, region, whisper)
        bodies.append(body)
    key = hashlib.sha256(repr(bodies).encode()).hexdigest()[:32]
    path = TTS_CACHE / f"vs-{key}.mp3"
    if path.exists():
        return path.read_bytes()
    audio = bytearray()
    for body in bodies:  # MP3 frames concatenate cleanly, so the runs play as one clip
        try:
            r = await client.post(f"{VOICESTUDIO_URL}/v1/audio/speech", json=body, headers=_vs_headers(),
                                  timeout=VOICESTUDIO_TIMEOUT)
        except httpx.RequestError as e:
            raise VoiceError(f"VoiceStudio isn't running at {VOICESTUDIO_URL}.") from e
        if r.status_code != 200:
            try:
                err = r.json().get("error", {})
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                msg = r.text[:200]
            raise VoiceError(f"VoiceStudio couldn't read it: {msg}")
        audio.extend(r.content)
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(audio))
    return bytes(audio)
