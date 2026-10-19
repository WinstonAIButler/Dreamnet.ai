"""
Dreamnet - detailed character profiles
The questions a writer needs answered to know a character, grouped by section.
Every answer can be public (known to the user's character, safe to mention) or
secret (true, but hidden, revealed only when the story earns it).
"""
import json

# (key, label, hint). Keys are stored in characters.profile as
# {"key": {"value": "...", "secret": false}}.
SECTIONS = [
    ("Look", [
        ("height_build", "Height and build", "6'3\", broad shoulders, a fighter's frame"),
        ("face", "Face and features", "Sharp jaw, a nose broken once, a slow smile"),
        ("eyes", "Eyes", "Storm grey, unreadable"),
        ("hair", "Hair", "Black, swept back, silver at the temples"),
        ("skin", "Skin and complexion", "Olive, sun-warmed; or pale as marble"),
        ("scars", "Scars and marks", "A knife scar along the left ribs"),
        ("tattoos", "Tattoos", "Placement, design and what each one means"),
        ("piercings", "Piercings", "Where, what jewellery"),
        ("style", "Clothes and style", "Tailored black suits, no tie, a signet ring"),
        ("scent", "Scent", "Cedar, smoke, expensive cologne"),
        ("voice_manner", "Voice and way of speaking", "Low, unhurried, slips into Italian when angry"),
    ]),
    ("Personality", [
        ("temperament", "Temperament", "Cold control over a violent temper"),
        ("desires", "What they want most", ""),
        ("fears", "Fears", ""),
        ("flaws", "Flaws", ""),
        ("habits", "Habits and tells", "Turns his ring when he's lying"),
        ("likes", "Likes", ""),
        ("dislikes", "Dislikes", ""),
        ("affection", "How they show love or obsession", ""),
        ("turn_ons", "Turn-ons", "Between consenting adults"),
        ("limits", "Hard limits", "What they will never do"),
    ]),
    ("Life", [
        ("job", "Job or business", "Runs the family's shipping company and its other trade"),
        ("wealth", "Wealth and money", "Old money; or self-made; or in debt to the wrong people"),
        ("home", "House or apartment", "Penthouse over the harbour, glass and black marble"),
        ("car", "Car or vehicles", "Matte black Aston Martin, a bike he never mentions"),
        ("origin", "Origin and nationality", "Born in Palermo, raised in New York"),
        ("languages", "Languages", ""),
        ("family", "Family", ""),
        ("allies", "Friends and allies", ""),
        ("enemies", "Enemies and rivals", ""),
        ("skills", "Skills", ""),
        ("weapons", "Weapons", ""),
        ("routine", "Daily routine", ""),
    ]),
    ("Past", [
        ("backstory", "Backstory", "Where they came from and what made them"),
        ("turning_point", "The moment that changed them", ""),
        ("past_loves", "Past loves", ""),
    ]),
    ("Supernatural (optional)", [
        ("species", "What they are", "Vampire, born 1712; or human"),
        ("powers", "Powers", "Described in lifelike, grounded terms"),
        ("weaknesses", "Weaknesses", ""),
        ("needs", "Needs and rules", "Feeding, the moon, bargains, curses"),
    ]),
    ("With you", [
        ("relationship", "Who you are to them", "The woman who owes him; his bodyguard's charge"),
        ("how_met", "How you met", ""),
        ("feelings", "What they feel about you", ""),
    ]),
]
FIELDS = {key: label for _, fields in SECTIONS for key, label, _ in fields}


def clean(raw) -> dict:
    """Keep known fields, trimmed, as {"value", "secret"}; drop empty ones."""
    out = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raw = {}
    for key, item in (raw or {}).items():
        if key not in FIELDS:
            continue
        if isinstance(item, str):
            item = {"value": item}
        value = str((item or {}).get("value") or "").strip()[:1500]
        if value:
            out[key] = {"value": value, "secret": bool(item.get("secret"))}
    return out


def for_prompt(profile: dict, name: str, username: str) -> str:
    """The profile as the roleplay model reads it: public details, then secrets."""
    public = [f"- {FIELDS[k]}: {v['value']}" for k, v in profile.items() if not v["secret"]]
    secret = [f"- {FIELDS[k]}: {v['value']}" for k, v in profile.items() if v["secret"]]
    text = ""
    if public:
        text += f"\nPublic details about {name} ({username} knows or can see these):\n" + "\n".join(public) + "\n"
    if secret:
        text += (f"\nSecrets about {name}. They are true and shape how {name} acts, but {name} hides them: never "
                 f"state them outright or let {username} learn them easily. Only hint, deflect or lie, and reveal "
                 "one only when the story has truly earned it:\n" + "\n".join(secret) + "\n")
    return text


def looks(profile: dict) -> str:
    """Visible physical details, for portraits (secret ones included only if they'd show)."""
    keys = ("height_build", "face", "eyes", "hair", "skin", "scars", "tattoos", "piercings", "style")
    return " ".join(f"{FIELDS[k]}: {profile[k]['value']}." for k in keys if k in profile)
