"""
Dreamnet - safety checks
Hard content lines that apply to every character and every message:
no minors, no real people, and a crisis hand-off for self-harm talk
(the crisis phrase list is adapted from JOSIE's JosieBrain).
These are keyword filters: a first line of defence, not a replacement for a
moderation model and human review.
"""
import re

MIN_CHARACTER_AGE = 18

# Terms that only ever describe minors or sexualised youth. Checked on every
# character definition and every chat message.
_STRONG_TERMS = [
    "underage", "under-age", "preteen", "pre-teen", "tween", "loli", "lolicon",
    "shota", "shotacon", "toddler", "jailbait", "pubescent", "prepubescent",
    "schoolgirl", "schoolboy", "middle schooler", "elementary schooler",
    "junior high", "high schooler", "high-schooler", "grade schooler",
]
# Broader words that are fine in everyday chat ("my kid", "a minor detail")
# but have no place in a companion's own definition.
_CHARACTER_TERMS = _STRONG_TERMS + [
    "child", "children", "childlike", "kid", "kids", "minor", "teen", "teens",
    "teenager", "teenage", "little girl", "little boy", "infant", "baby-faced",
    "high school", "middle school", "elementary school", "primary school", "grade school",
]


def _terms_re(terms):
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)


_STRONG_RE = _terms_re(_STRONG_TERMS)
_CHARACTER_RE = _terms_re(_CHARACTER_TERMS)

# "15 years old", "15-year-old", "15yo", "16 y/o", "age 12", "aged 12".
_AGE_RE = re.compile(
    r"\b(?:(\d{1,2})\s*-?\s*(?:years?|yrs?)\s*-?\s*old|(\d{1,2})\s*y/?o|aged?\s+(?:of\s+)?(\d{1,2}))\b",
    re.I)
# "she is 15", "he's 16", "I'm only 17", "they were 14": a person plus a bare number, not a height or time.
_IS_AGE_RE = re.compile(
    r"\b(?:she|he|they|i|you|we)\s*(?:is|was|are|were|am|'s|'m|'re)\s+(?:only\s+|just\s+|barely\s+)?(\d{1,2})"
    r"(?![\d'\"’]|\s*(?:feet|foot|ft|inch|inches|cm|m\b|kg|lbs?|pounds|minutes?|mins?|hours?|o'clock|am\b|pm\b|%|percent|"
    r"times|steps|miles|km|meters|metres|days|weeks|months|seconds|secs|of\b|out\b|to\b|against\b|vs\b|down\b|up\b|ahead\b|behind\b|-))", re.I)

_CRISIS_PHRASES = [
    "kill myself", "killing myself", "want to die", "wanna die",
    "going to end my life", "thinking about suicide", "thinking about killing myself",
    "i should just die", "i should kill myself", "better off dead",
    "better off without me", "don't want to live", "dont want to live",
    "no reason to live", "can't go on", "cant go on", "end it all",
    "end my life", "take my own life", "cut myself", "hurt myself", "harm myself",
    "self harm", "self-harm", "overdose on", "kill myself with", "suicide note",
    "i'm suicidal", "im suicidal", "feeling suicidal",
]

CRISIS_RESPONSE = (
    "I'm stepping out of our story for a second, because this matters more.\n\n"
    "You don't have to be okay right now, but please reach out to someone who can "
    "really be there for you:\n\n"
    "- US: call or text 988 (Suicide & Crisis Lifeline)\n"
    "- UK and Ireland: Samaritans on 116 123\n"
    "- Anywhere else: https://findahelpline.com\n\n"
    "I'm an AI, and I'm still here when you want to talk. But please talk to one of them first."
)


def mentions_minor(text: str, strict: bool = False) -> bool:
    """True if text describes someone under 18. ``strict`` adds the broader
    word list used for character definitions."""
    if not text:
        return False
    if (_CHARACTER_RE if strict else _STRONG_RE).search(text):
        return True
    for m in list(_AGE_RE.finditer(text)) + list(_IS_AGE_RE.finditer(text)):
        age = next((g for g in m.groups() if g), None)
        if age and int(age) < MIN_CHARACTER_AGE:
            return True
    return False


def is_crisis(text: str) -> bool:
    lower = (text or "").lower()
    return any(p in lower for p in _CRISIS_PHRASES)


def check_character(fields: dict) -> str | None:
    """Return an error message if a character definition breaks the rules."""
    try:
        age = int(fields.get("age") or 0)
    except (TypeError, ValueError):
        return "Age must be a number."
    if age < MIN_CHARACTER_AGE:
        return f"Characters must be at least {MIN_CHARACTER_AGE} years old."
    for key in ("name", "tagline", "persona", "greeting", "appearance", "tags"):
        if mentions_minor(str(fields.get(key) or ""), strict=True):
            return "Characters can't be described as minors or as childlike."
    return None


# Appended to every system prompt so the model holds the same lines.
SAFETY_RULES = (
    "Hard rules that override everything else: every character in this story is an adult "
    f"aged {MIN_CHARACTER_AGE} or over, and you never depict, imply or role-play anyone under "
    f"{MIN_CHARACTER_AGE}. You never role-play as a real, identifiable person. If the user asks "
    "whether you are an AI, you say yes. If the user talks about hurting themselves, you drop the "
    "role-play and point them to a crisis line."
)
