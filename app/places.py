"""
Dreamnet - starting places
Where a new roleplay scene can begin, grouped like a location picker. The
character's own places (their home, where they work, their car) come first,
taken from their profile.
"""
import app.profile as profiles

# (group, [place])
GROUPS = [
    ("City nights", [
        "A penthouse with floor-to-ceiling windows over the city",
        "The VIP booth of an exclusive nightclub",
        "A rooftop bar after closing",
        "A rain-soaked alley behind the club",
        "The high-stakes room of a private casino",
        "A luxury hotel suite",
        "An underground fight club",
        "A tattoo parlour after hours",
    ]),
    ("Old money and shadows", [
        "The library of a gothic mansion",
        "A masquerade ball",
        "An empty cathedral at midnight",
        "The great hall of a remote castle",
        "A candlelit wine cellar",
        "A private art gallery after closing",
        "A jazz lounge with a piano in the corner",
    ]),
    ("Danger", [
        "An abandoned warehouse by the docks",
        "A safehouse outside the city",
        "An interrogation room",
        "The back seat of a car, speeding through the night",
        "The docks at midnight",
        "A back room where a deal just went wrong",
    ]),
    ("Escape", [
        "A private yacht far from shore",
        "A cabin deep in snowy woods",
        "A moonlit forest clearing",
        "A villa on a private island",
        "A private jet above the clouds",
        "A sleeper cabin on an overnight train",
    ]),
    ("Everyday", [
        "Your apartment, late at night",
        "A quiet corner of a coffee shop",
        "An office after everyone has gone home",
        "A dusty bookstore",
        "An empty gym",
        "A hospital room",
        "A late-night convenience store",
    ]),
]


def for_character(c: dict) -> list[str]:
    """Places from the character's own profile: home, work, car."""
    prof = profiles.clean(c.get("profile"))
    name = c.get("name") or "Their"
    out = []
    if "home" in prof:
        out.append(f"{name}'s home: {prof['home']['value']}")
    if "job" in prof:
        out.append(f"Where {name} works: {prof['job']['value']}")
    if "car" in prof:
        out.append(f"In {name}'s car: {prof['car']['value']}")
    return [p[:300] for p in out]


def catalog(c: dict | None = None) -> list[dict]:
    groups = [{"group": g, "places": [{"place": p} for p in items]} for g, items in GROUPS]
    own = for_character(c) if c else []
    if own:
        groups.insert(0, {"group": f"{c['name']}'s world", "places": [{"place": p} for p in own]})
    return groups
