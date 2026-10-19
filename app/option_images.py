"""
Dreamnet - photos for choice tiles
The character creator and the starting-place picker show a realistic photo on
every choice. Photos are made once, one at a time, and kept on disk under
PORTRAIT_DIR/options: on your own ComfyUI (the same Z-Image workflow as
portraits) when COMFYUI_URL is set, otherwise by an online image service
(OPTION_IMAGE_SERVICE, Pollinations by default; set it to "off" to show names only).
"""
import asyncio
import hashlib
import os
from urllib.parse import quote

import app.portraits as portraits

OPTION_DIR = portraits.PORTRAIT_DIR / "options"
TILE = (640, 800)        # people and things, 4:5
PLACE = (832, 560)       # places, about 3:2

_STYLE = ("Photorealistic photograph, shot on a full-frame camera, natural skin texture, real light, "
          "cinematic low-key lighting with deep navy and plum tones and a warm coral rim light, shallow depth of "
          "field. Not a drawing, not anime, not CGI, not an illustration, no text, no logo.")
_WHO = {"male": "a man", "female": "a woman", "non-binary": "an androgynous person"}

# set: (template, size). {key} is the choice, {who} the character's gender.
TEMPLATES = {
    "gender": ("Editorial portrait of {key_who} in their late twenties, dark fitted clothes, confident gaze.", TILE),
    "kind": ("Portrait of {who} in their late twenties who is a {key}, shown in a grounded, lifelike way: subtle "
             "practical makeup and real physical detail only, dark elegant clothes, fully clothed.", TILE),
    "heritage": ("Portrait of {who} in their late twenties of {key} heritage, dark elegant clothes, fully clothed.", TILE),
    "hair_style": ("Portrait of {who} in their late twenties with {key} hair, dark clothes, fully clothed.", TILE),
    "build": ("Full-body fashion photograph of {who} in their late twenties with a {key} build, fitted dark "
              "clothes, fully clothed, standing in a dim studio.", TILE),
    "height": ("Full-body photograph of {who} in their late twenties who is {key}, standing in a doorway for scale, "
               "fully clothed in dark clothes.", TILE),
    "personality": ("Portrait of {who} in their late twenties whose expression and body language read as {key}, "
                    "dark clothes, fully clothed.", TILE),
    "job": ("Environmental portrait of {who} in their thirties working as a {key}, in their workplace, dressed for "
            "the job, fully clothed.", TILE),
    "relationship": ("Moody photograph suggesting a {key} relationship between two adults in their thirties, seen "
                     "from a distance, fully clothed, faces partly in shadow.", TILE),
    "hobbies": ("Photograph of {who} in their late twenties enjoying {key}, fully clothed, candid.", TILE),
    "turn_ons": ("Tasteful still-life photograph with no people that evokes {key}: objects, fabric and light on "
                 "dark silk, sensual but not explicit.", TILE),
    "place": ("Photograph of {key}, empty, no people, atmospheric, at night unless the place says otherwise.", PLACE),
}

# {prompt}, {width}, {height} and {seed} are filled in. Only the generic choice prompt is sent,
# never anything from your characters or chats.
ONLINE_SERVICE = os.getenv("OPTION_IMAGE_SERVICE", "https://image.pollinations.ai/prompt/{prompt}"
                           "?width={width}&height={height}&seed={seed}&nologo=true&private=true&safe=true&model=flux")
ONLINE_TIMEOUT = float(os.getenv("OPTION_IMAGE_TIMEOUT", "120"))

_lock = asyncio.Lock()  # one picture at a time, so ComfyUI isn't flooded


class OptionImageError(Exception):
    pass


def prompt_for(kind: str, key: str, gender: str = "") -> tuple[str, tuple[int, int]]:
    template, size = TEMPLATES[kind]
    who = _WHO.get(gender, "a person")
    text = template.format(key=key.lower(), who=who, key_who=_WHO.get(key, "a person"))
    return f"{text} Every person shown is an adult. {_STYLE}", size


def path_for(prompt: str):
    return OPTION_DIR / (hashlib.sha256(prompt.encode()).hexdigest()[:24] + ".png")


async def get(client, kind: str, key: str, gender: str = "") -> bytes:
    """The photo for a choice: from disk, or made now on ComfyUI and kept."""
    prompt, (w, h) = prompt_for(kind, key, gender)
    path = path_for(prompt)
    if path.exists():
        return path.read_bytes()
    online = not portraits.enabled()
    if online and ONLINE_SERVICE.strip().lower() in ("", "off", "none"):
        raise OptionImageError("Choice photos are turned off.")
    async with _lock:
        if path.exists():  # made while this request waited
            return path.read_bytes()
        seed = int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)
        if online:
            png = await _online(client, prompt, w, h, seed)
        else:
            try:
                png = await portraits.generate(client, prompt, seed=seed, width=w, height=h)
            except portraits.PortraitError as e:
                raise OptionImageError(str(e)) from e
        OPTION_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return png


async def _online(client, prompt: str, w: int, h: int, seed: int) -> bytes:
    url = ONLINE_SERVICE.format(prompt=quote(prompt, safe=""), width=w, height=h, seed=seed)
    try:
        r = await client.get(url, timeout=ONLINE_TIMEOUT, follow_redirects=True)
    except Exception as e:
        raise OptionImageError(f"The online image service didn't answer: {e}") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") or len(r.content) < 2000:
        raise OptionImageError(f"The online image service couldn't make this picture ({r.status_code}).")
    return r.content
