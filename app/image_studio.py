"""
Dreamnet - image creator
Choices for the "Create image" page and the prompt they build. Each picture shows one of the user's
characters, always an adult, as a realistic photograph: the character's own looks come from their card,
and the scene, outfit, pose and shape come from the page.
"""

# (label, how it reads in the prompt)
SCENES = [
    ("Penthouse at night", "a dim luxury penthouse at night, city lights through floor-to-ceiling windows"),
    ("Candlelit bedroom", "a candlelit bedroom with dark silk sheets and heavy velvet curtains"),
    ("Rainy street", "a rain-soaked city street at night, neon reflections on wet asphalt"),
    ("Private library", "an old private library with leather armchairs and a crackling fireplace"),
    ("Nightclub", "a crowded underground nightclub, smoky air and coloured stage light"),
    ("Gothic cathedral", "a gothic cathedral nave lit by candles and stained glass"),
    ("Moonlit forest", "a misty forest at night under a full moon"),
    ("Luxury car", "the leather back seat of a black luxury car at night"),
    ("Hotel suite", "an elegant hotel suite in soft warm lamplight"),
    ("Beach at sunset", "an empty beach at sunset, warm golden light on the water"),
    ("Rooftop", "a city rooftop at blue hour, the skyline behind"),
    ("Office after hours", "a glass corner office after hours, desk lamp on, city below"),
    ("Mansion staircase", "the grand marble staircase of a dark mansion"),
    ("Bathtub", "a marble bathroom with a deep bathtub, steam and candlelight"),
    ("Kitchen morning", "a sunlit modern kitchen in the morning"),
    ("Studio backdrop", "a plain dark studio backdrop with a single soft key light"),
]

OUTFITS = [
    ("As they are", ""),
    ("Tailored suit", "a sharply tailored dark suit"),
    ("Evening gown", "a floor-length silk evening gown"),
    ("Little black dress", "a fitted little black dress"),
    ("Leather jacket", "a worn black leather jacket over dark clothes"),
    ("Casual", "casual jeans and a fitted top"),
    ("Oversized shirt", "only an oversized white button-down shirt"),
    ("Silk robe", "a loosely tied silk robe"),
    ("Lingerie", "elegant black lace lingerie"),
    ("Swimwear", "stylish swimwear"),
    ("Workout", "fitted workout clothes"),
    ("Uniform", "a uniform that fits their job"),
    ("Masquerade", "a masquerade ball outfit with a lace eye mask"),
    ("Gothic", "dark gothic clothing with lace and corsetry"),
    ("Pajamas", "soft silk pajamas"),
]

POSES = [
    ("Portrait", "a head-and-shoulders portrait, looking into the camera"),
    ("Standing", "standing full length, relaxed and confident"),
    ("Sitting", "sitting, leaning back, one arm draped over the chair"),
    ("Lying down", "lying back on the bed or sofa, looking up at the camera"),
    ("Over the shoulder", "glancing back over one shoulder"),
    ("Leaning in", "leaning close toward the camera, intimate eye contact"),
    ("Against the wall", "leaning against the wall, arms crossed"),
    ("Walking", "walking toward the camera, mid-stride"),
    ("Mirror selfie", "taking a mirror selfie with a phone"),
    ("Candid laugh", "a candid moment, laughing, not looking at the camera"),
    ("Brooding", "a brooding look, eyes in shadow"),
    ("Kneeling", "kneeling on the bed, looking at the camera"),
]

SHAPES = {
    "portrait": ("Portrait", 896, 1152),
    "square": ("Square", 1024, 1024),
    "landscape": ("Landscape", 1152, 896),
    "wide": ("Wide", 1344, 768),
}

MAX_COUNT = 4


def options() -> dict:
    return {
        "scenes": [s[0] for s in SCENES],
        "outfits": [o[0] for o in OUTFITS],
        "poses": [p[0] for p in POSES],
        "shapes": [{"key": k, "label": v[0], "width": v[1], "height": v[2]} for k, v in SHAPES.items()],
        "max_count": MAX_COUNT,
    }


def _pick(table, label: str) -> str:
    for name, text in table:
        if name == label:
            return text
    return ""


def build_prompt(c: dict, looks: str, scene: str, outfit: str, pose: str, details: str, avoid: str) -> str:
    """Prompt for one picture of character c. Choices outside the lists are ignored; details is free text."""
    age = int(c["age"])
    who = f"a {age}-year-old {c['gender']}" if age <= 100 else f"an ageless {c['gender']} who looks about 35"
    parts = [f"Photorealistic photograph of {who}."]
    appearance = " ".join(x for x in ((c.get("appearance") or "").strip(), looks.strip()) if x)
    if appearance:
        parts.append(f"Their looks, exactly: {appearance}")
    pose_text = _pick(POSES, pose) or POSES[0][1]
    parts.append(f"Pose: {pose_text}.")
    outfit_text = _pick(OUTFITS, outfit)
    if outfit_text:
        parts.append(f"Wearing {outfit_text}.")
    scene_text = _pick(SCENES, scene)
    if scene_text:
        parts.append(f"Setting: {scene_text}.")
    if details.strip():
        parts.append(details.strip().rstrip(".") + ".")
    if c.get("tagline"):
        parts.append(f"Mood that fits: {c['tagline']}.")
    parts.append(
        "An adult, tasteful and sensual, no explicit nudity. It must look like a real photograph of a real person: "
        "natural skin with pores and fine imperfections, real light, cinematic colour grade, 50mm or 85mm lens, "
        "shallow depth of field, high detail. Not a drawing, not anime, not CGI, not a cartoon."
    )
    if avoid.strip():
        parts.append(f"Leave out: {avoid.strip()}.")
    return " ".join(parts)
