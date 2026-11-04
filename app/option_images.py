"""
Dreamnet - photos for choice tiles
The character creator and the starting-place picker show a realistic photo on
every choice. Photos are made once, one at a time, and kept on disk under
PORTRAIT_DIR/options: on your own ComfyUI or cloud GPU (the same Z-Image workflow as
portraits) when one is set up, otherwise by an online image service
(OPTION_IMAGE_SERVICE, Pollinations by default; set it to "off" to show names only).

GPU photos and online photos are kept apart ("-gpu" / "-web" files), so once a GPU is connected its
realistic photos replace the online ones. When the GPU is off, the online photos are shown instead.
bake() makes every tile on the GPU in one go (the "Make realistic trait photos" button).
"""
import asyncio
import hashlib
import os
import time
from urllib.parse import quote

import app.gpu as gpu
import app.portraits as portraits

OPTION_DIR = portraits.PORTRAIT_DIR / "options"
TILE = (640, 800)        # people and things, 4:5
PLACE = (832, 560)       # places, about 3:2
SWATCH = (640, 640)      # eye and skin close-ups, square

_STYLE = ("RAW photo, real person, editorial beauty photography, 85mm portrait lens at f/2, sharp focus on the "
          "eyes, natural skin texture with pores and fine hairs, true-to-life colours, soft diffused studio light, "
          "plain backdrop in dusty rose fading to deep plum. Not a drawing, not anime, not CGI, not 3D, not an "
          "illustration, no text, no logo, no watermark.")
_MACRO = ("RAW macro photograph, real human, 100mm macro lens, razor-sharp detail, true-to-life colours, soft "
          "diffused light. Not a drawing, not CGI, not 3D, not an illustration, no text, no logo, no watermark.")
_WHO = {"male": "a man", "female": "a woman", "trans woman": "a transgender woman", "trans man": "a transgender man",
        "non-binary": "an androgynous person"}

# set: (template, size). {key} is the choice, {who} the character's gender.
TEMPLATES = {
    "gender": ("Editorial portrait of {key_who} in their late twenties, dark fitted clothes, confident gaze.", TILE),
    "kind": ("Portrait of {who} in their late twenties who is a {key}, shown in a grounded, lifelike way: subtle "
             "practical makeup and real physical detail only, dark elegant clothes, fully clothed.", TILE),
    "heritage": ("Portrait of {who} in their late twenties of {key} heritage, dark elegant clothes, fully clothed.", TILE),
    "hair_style": ("Portrait of {who} in their late twenties with {key} hair, dark clothes, fully clothed.", TILE),
    "hair": ("Close-up portrait of {who} in their late twenties with natural {key} hair, the hair colour clearly "
             "visible and true to life in soft even light, dark clothes, fully clothed.", TILE),
    "eyes": ("Macro photograph of one human eye with a {key} iris, filling the frame: the iris colour vivid, sharp "
             "and true to life, fine iris texture, natural eyelashes and a catchlight (coloured contact lens if the "
             "colour is unusual).", SWATCH),
    "skin": ("Macro close-up photograph of human skin in a {key} skin tone, the tone true to life in soft neutral "
             "light: the curve of a shoulder and collarbone, natural texture and pores, no makeup.", SWATCH),
    "build": ("Full-body fashion photograph of {who} in their late twenties with a {key} build, fitted dark "
              "clothes, fully clothed, standing in a dim studio.", TILE),
    "chest": ("Waist-up fashion photograph of {who} in their late twenties with a {key} bust (flat if it says flat), "
              "in a fitted dark top, fully clothed, natural proportions.", TILE),
    "butt": ("Full-body fashion photograph from behind of {who} in their late twenties with a {key} butt, looking "
             "back over one shoulder, in fitted dark jeans, fully clothed, natural proportions.", TILE),
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
    return f"{text} Every person shown is an adult. {_MACRO if size == SWATCH else _STYLE}", size


def path_for(prompt: str, src: str = "web"):
    base = hashlib.sha256(prompt.encode()).hexdigest()[:24]
    return OPTION_DIR / f"{base}-{src}.png"


def _legacy_path(prompt: str):
    return OPTION_DIR / (hashlib.sha256(prompt.encode()).hexdigest()[:24] + ".png")


_gpu_down_until = 0.0  # after a GPU failure, skip it for a minute so tiles fall back fast


def _seed(kind: str, prompt: str, gender: str) -> int:
    # One seed per set and gender, so every tile in a step shows the same model and only the
    # chosen trait changes between them (places each get their own).
    basis = prompt if kind == "place" else f"{kind}|{gender}"
    return int(hashlib.sha256(basis.encode()).hexdigest()[:8], 16)


async def _make_gpu(client, kind: str, prompt: str, gender: str, w: int, h: int) -> bytes:
    global _gpu_down_until
    try:
        png = await portraits.generate(client, prompt, seed=_seed(kind, prompt, gender), width=w, height=h)
    except portraits.PortraitError:
        _gpu_down_until = time.monotonic() + 60
        raise
    path = path_for(prompt, "gpu")
    OPTION_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return png


async def get(client, kind: str, key: str, gender: str = "") -> tuple[bytes, str]:
    """The photo for a choice and where it came from ("gpu" or "web"): from disk, or made now and kept."""
    prompt, (w, h) = prompt_for(kind, key, gender)
    gpu_path = path_for(prompt, "gpu")
    if gpu_path.exists():
        return gpu_path.read_bytes(), "gpu"
    async with _lock:
        if gpu_path.exists():  # made while this request waited
            return gpu_path.read_bytes(), "gpu"
        if gpu.configured() and time.monotonic() >= _gpu_down_until:
            try:
                return await _make_gpu(client, kind, prompt, gender, w, h), "gpu"
            except portraits.PortraitError:
                pass  # GPU off or busy: fall back to an online photo
        for p in (path_for(prompt, "web"), _legacy_path(prompt)):
            if p.exists():
                return p.read_bytes(), "web"
        if ONLINE_SERVICE.strip().lower() in ("", "off", "none"):
            raise OptionImageError("Choice photos are turned off." if not gpu.configured()
                                   else "The GPU is off, so this photo can't be made right now.")
        png = await _online(client, prompt, w, h, _seed(kind, prompt, gender))
        OPTION_DIR.mkdir(parents=True, exist_ok=True)
        path_for(prompt, "web").write_bytes(png)
        return png, "web"


# --- make every tile on the GPU in one go ---

bake_state = {"running": False, "done": 0, "made": 0, "total": 0, "failed": 0, "problem": "", "finished_at": 0}


def bake_plan(items: list) -> list[tuple[str, tuple[int, int], str, str]]:
    """Unique prompts still missing a GPU photo, from [set, key, gender] items."""
    seen, plan = set(), []
    for it in items:
        try:
            kind, key, gender = str(it[0]), " ".join(str(it[1]).split())[:200], str(it[2] if len(it) > 2 else "")
        except (TypeError, IndexError):
            continue
        if kind not in TEMPLATES or not key:
            continue
        prompt, size = prompt_for(kind, key, gender)
        if prompt in seen:
            continue
        seen.add(prompt)
        plan.append((prompt, size, kind, gender))
    return plan


async def bake(client, items: list):
    """Make every missing tile on the GPU, one at a time. Stops after 5 failures in a row."""
    plan = bake_plan(items)
    bake_state.update(running=True, done=0, made=0, total=len(plan), failed=0, problem="", finished_at=0)
    streak = 0
    try:
        for prompt, (w, h), kind, gender in plan:
            if path_for(prompt, "gpu").exists():
                bake_state["done"] += 1
                continue
            async with _lock:
                try:
                    await _make_gpu(client, kind, prompt, gender, w, h)
                    bake_state["made"] += 1
                    streak = 0
                except portraits.PortraitError as e:
                    bake_state["failed"] += 1
                    bake_state["problem"] = str(e)
                    streak += 1
            bake_state["done"] += 1
            if streak >= 5:
                bake_state["problem"] = "Stopped: " + bake_state["problem"]
                break
    finally:
        bake_state.update(running=False, finished_at=time.time())


async def _online(client, prompt: str, w: int, h: int, seed: int) -> bytes:
    url = ONLINE_SERVICE.format(prompt=quote(portraits.online_prompt(prompt), safe=""), width=w, height=h, seed=seed)
    try:
        r = await portraits.online_get(client, url, ONLINE_TIMEOUT)
    except Exception as e:
        raise OptionImageError(f"The online image service didn't answer: {e}") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") or len(r.content) < 2000:
        raise OptionImageError(f"The online image service couldn't make this picture ({r.status_code}).")
    return r.content
