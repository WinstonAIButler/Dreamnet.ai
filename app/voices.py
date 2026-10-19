"""
Dreamnet - read-aloud voices
English neural voices grouped by country, spoken through Microsoft Edge's online
Read Aloud service (the edge-tts package). Needs an internet connection; when it
isn't available the web app falls back to the English voices on your own device.
"""
import hashlib
import os
import re
from pathlib import Path

try:
    import edge_tts
except ImportError:  # optional dependency
    edge_tts = None

TTS_CACHE = Path(os.getenv("TTS_CACHE", "tts_cache"))

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

# What to read aloud from a roleplay reply.
READ_MODES = ("all", "words_thoughts", "words")


class VoiceError(Exception):
    pass


def enabled() -> bool:
    return edge_tts is not None


def speakable(text: str, mode: str = "all") -> str:
    """Turn roleplay markup into plain text to read: "words", *actions*, `thoughts`."""
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
        out.append(p.strip())
    spoken = " ".join(out)
    return re.sub(r"\s+", " ", spoken).strip()[:5000]


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
