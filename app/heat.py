"""
Dreamnet - how much a picture or video may show ("picture heat").

Three levels, set on the Create image page and saved in studio_settings:
- tasteful: intimacy through closeness, touch, sheets or shadow, no nudity (the default)
- sensual: lingerie, bare skin and implied nudity, no explicit sex
- explicit: nudity and sex between the consenting adults in the scene

Sensual and explicit only apply when pictures and videos are made on your own ComfyUI or cloud GPU.
Online services (the free image service, Replicate) always get the tasteful rule.
Whatever the level: every person is an adult, and never a real, identifiable person.
"""
import app.gpu as gpu

LEVELS = ("tasteful", "sensual", "explicit")
LABELS = {"tasteful": "Tasteful", "sensual": "Sensual", "explicit": "Explicit"}

ALWAYS = ("Every person is clearly an adult, 18 or older, with an adult face and body. "
          "Never a real or identifiable person.")

RULES = {
    "tasteful": "Keep it tasteful: if the moment is intimate, show it through closeness, touch, expressions, "
                "sheets or shadow, with no nudity.",
    "sensual": "Sensual and suggestive: lingerie, bare skin and implied nudity are fine, and intimacy shows "
               "through closeness and touch, but no explicit sex and no exposed genitals.",
    "explicit": "Explicit adult content is allowed: nudity and sex between the consenting adults in the scene, "
                "shown as the moment describes.",
}

_level = "tasteful"


def set_level(level: str):
    global _level
    if level not in LEVELS:
        raise ValueError("Pick tasteful, sensual or explicit.")
    _level = level


def chosen() -> str:
    """The level picked in settings, whether or not it can apply right now."""
    return _level


def level(online: bool = False) -> str:
    """The level that applies to a picture: anything above tasteful needs your own GPU."""
    if online or not gpu.own_gpu():
        return "tasteful"
    return _level


def rule(online: bool = False) -> str:
    """The sentence(s) added to every picture or video prompt."""
    return f"{ALWAYS} {RULES[level(online)]}"


def photo_note() -> str:
    """What a character is told when the user asks them for a photo in chat."""
    lv = level()
    if lv == "tasteful":
        return "Keep it tasteful."
    if lv == "sensual":
        return "It can be sensual and revealing, but not explicit."
    return "It can be as explicit as the moment is."
