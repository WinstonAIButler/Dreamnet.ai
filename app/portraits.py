"""
Dreamnet - lifelike character portraits
Generates photorealistic portraits through a ComfyUI server, using the
Z-Image-Turbo workflow from Intel AI Playground's "Pro 2 Image" preset
(modes/base/presets/z-image.json), its most photorealistic image preset.
Point COMFYUI_URL at AI Playground's own ComfyUI or any ComfyUI install that
has the Z-Image-Turbo model files.
"""
import asyncio
import copy
import os
import random
import secrets
from pathlib import Path

import httpx

COMFYUI_URL = os.getenv("COMFYUI_URL", "").rstrip("/")
PORTRAIT_DIR = Path(os.getenv("PORTRAIT_DIR", "portraits"))
PORTRAIT_TIMEOUT = float(os.getenv("PORTRAIT_TIMEOUT", "300"))

# Model file names as ComfyUI lists them. The defaults match a plain ComfyUI
# install from Comfy-Org/z_image_turbo; AI Playground stores them under
# "Comfy-Org---z_image_turbo\split_files\..." (see README).
ZIMAGE_UNET = os.getenv("ZIMAGE_UNET", "z_image_turbo_bf16.safetensors")
ZIMAGE_CLIP = os.getenv("ZIMAGE_CLIP", "qwen_3_4b.safetensors")
ZIMAGE_VAE = os.getenv("ZIMAGE_VAE", "ae.safetensors")

# Without ComfyUI (as on a hosted server), character portraits are made by an online image
# service instead. {prompt}, {width}, {height} and {seed} are filled in. Only the portrait prompt
# written from the character card is sent, never chats. Set PORTRAIT_SERVICE=off to turn it off.
ONLINE_SERVICE = os.getenv("PORTRAIT_SERVICE", "https://image.pollinations.ai/prompt/{prompt}"
                           "?width={width}&height={height}&seed={seed}&nologo=true&private=true&safe=true&model=flux")
ONLINE_TIMEOUT = float(os.getenv("PORTRAIT_SERVICE_TIMEOUT", "180"))
ONLINE_PROMPT_CHARS = 1500  # keeps the request URL a sane length

# 3:4 portrait at about 1 megapixel, from the preset's resolution table.
WIDTH, HEIGHT = 896, 1152
# Scene pictures are landscape, 3:2-ish at about 1 megapixel.
SCENE_WIDTH, SCENE_HEIGHT = 1216, 832

# AI Playground's Z-Image API workflow, with its AI Playground-only SafetyChecker
# nodes removed so it runs on stock ComfyUI. Node ids and titles are unchanged.
ZIMAGE_WORKFLOW = {
    "9": {"class_type": "SaveImage", "_meta": {"title": "Save Image"},
          "inputs": {"filename_prefix": "Dreamnet_Portrait", "images": ["43", 0]}},
    "39": {"class_type": "CLIPLoader", "_meta": {"title": "Load CLIP"},
           "inputs": {"clip_name": ZIMAGE_CLIP, "type": "lumina2", "device": "default"}},
    "40": {"class_type": "VAELoader", "_meta": {"title": "Load VAE"},
           "inputs": {"vae_name": ZIMAGE_VAE}},
    "41": {"class_type": "EmptySD3LatentImage", "_meta": {"title": "EmptySD3LatentImage"},
           "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
    "42": {"class_type": "ConditioningZeroOut", "_meta": {"title": "ConditioningZeroOut"},
           "inputs": {"conditioning": ["45", 0]}},
    "43": {"class_type": "VAEDecode", "_meta": {"title": "VAE Decode"},
           "inputs": {"samples": ["44", 0], "vae": ["40", 0]}},
    "44": {"class_type": "KSampler", "_meta": {"title": "KSampler"},
           "inputs": {"seed": 0, "steps": 9, "cfg": 1, "sampler_name": "res_multistep", "scheduler": "simple",
                      "denoise": 1, "model": ["47", 0], "positive": ["45", 0], "negative": ["42", 0],
                      "latent_image": ["41", 0]}},
    "45": {"class_type": "CLIPTextEncode", "_meta": {"title": "prompt"},
           "inputs": {"text": "", "clip": ["39", 0]}},
    "46": {"class_type": "UNETLoader", "_meta": {"title": "Load Diffusion Model"},
           "inputs": {"unet_name": ZIMAGE_UNET, "weight_dtype": "default"}},
    "47": {"class_type": "ModelSamplingAuraFlow", "_meta": {"title": "ModelSamplingAuraFlow"},
           "inputs": {"shift": 3, "model": ["46", 0]}},
}

# How AI Playground tells its chat model to write Z-Image prompts (toolInstructions
# in z-image.json), narrowed to a clothed character portrait.
PROMPT_INSTRUCTIONS = (
    "Write one detailed image prompt for Z-Image-Turbo, a photorealistic image model. Describe a single adult "
    "person as a head-and-shoulders portrait photograph: face, skin texture, hair, eyes, expression, clothing "
    "(fully clothed), pose, background, lighting, lens and mood. It must look like a real photograph of a real "
    "person: editorial or cinematic portrait photography, natural skin with pores and imperfections, 85mm lens, "
    "shallow depth of field. If the character is supernatural, show those traits subtly and realistically, as "
    "practical makeup or real physical detail, never cartoon, anime, CGI or illustration. Return only the prompt "
    "as one paragraph, no preamble."
)


# For a picture of a roleplay moment: the current message is the action, and the
# character details keep everyone looking the same from picture to picture.
SCENE_INSTRUCTIONS = (
    "Write one detailed image prompt for Z-Image-Turbo, a photorealistic image model, showing a single moment "
    "from a story as a cinematic film still. The CURRENT MOMENT is what the picture shows: its action, poses, "
    "expressions and where everyone is. Everything else (character details, earlier events, setting, time of "
    "day) is only there to keep it accurate: each character's face, hair, eyes, build, skin, scars, tattoos, "
    "piercings and clothing must match their details exactly, and the place, weather and light must fit the "
    "story so far and the time of day. Name no one; describe each person by their looks so the image model can "
    "draw them. Every person is an adult. Keep it tasteful: if the moment is intimate, show it through closeness, "
    "touch, expressions, sheets or shadow, with no explicit nudity; violence can be tense but not gory. It must "
    "look like a real photograph: natural skin texture, real light, 35mm or 50mm lens, cinematic colour grade, "
    "shallow depth of field. Supernatural traits are subtle and realistic, never cartoon, anime, CGI or "
    "illustration. Return only the prompt as one paragraph of at most 180 words, no preamble."
)


class PortraitError(Exception):
    pass


def enabled() -> bool:
    return bool(COMFYUI_URL)


def template_prompt(c: dict) -> str:
    """Prompt built straight from the character card, used when no LLM prompt is available."""
    who = f"a {c['age']}-year-old {c['gender']}" if int(c["age"]) <= 100 else f"an ageless {c['gender']} who looks about 35"
    return (
        f"Photorealistic head-and-shoulders editorial portrait photograph of {who}, {c['name']}. "
        f"{c.get('appearance') or ''} Expression and mood that fit: {c.get('tagline') or ''}. "
        "Fully clothed. Real skin texture with pores and fine imperfections, natural catchlights in the eyes, "
        "moody cinematic low-key lighting with warm and cool accents, dark softly blurred background, 85mm lens, "
        "f/1.8, shallow depth of field, shot on a full-frame camera, high detail, true-to-life colour. "
        "Not a drawing, not anime, not CGI."
    )


def build_workflow(prompt: str, seed: int | None = None, width: int = WIDTH, height: int = HEIGHT) -> dict:
    wf = copy.deepcopy(ZIMAGE_WORKFLOW)
    wf["41"]["inputs"].update(width=width, height=height)
    wf["45"]["inputs"]["text"] = prompt
    wf["44"]["inputs"]["seed"] = seed if seed is not None else random.randint(0, 2**48)
    return wf


def online_enabled() -> bool:
    return ONLINE_SERVICE.strip().lower() not in ("", "off", "none")


def can_make_portraits() -> bool:
    """Character portraits work with ComfyUI or, failing that, the online service."""
    return enabled() or online_enabled()


async def make_portrait(client: httpx.AsyncClient, prompt: str) -> bytes:
    """A character portrait: on ComfyUI when it's set up, otherwise from the online service."""
    if enabled():
        return await generate(client, prompt)
    if not online_enabled():
        raise PortraitError("Portrait generation is off. Upload a photo instead.")
    from urllib.parse import quote
    url = ONLINE_SERVICE.format(prompt=quote(prompt[:ONLINE_PROMPT_CHARS], safe=""), width=WIDTH, height=HEIGHT,
                                seed=random.randint(1, 2**31 - 1))
    try:
        r = await client.get(url, timeout=ONLINE_TIMEOUT, follow_redirects=True)
    except Exception as e:
        raise PortraitError(f"The online image service didn't answer ({type(e).__name__}). Try again.") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") or len(r.content) < 2000:
        raise PortraitError(f"The online image service couldn't make this portrait ({r.status_code}). Try again.")
    return r.content


async def generate(client: httpx.AsyncClient, prompt: str, seed: int | None = None,
                   width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """Queue the workflow on ComfyUI, wait for it, and return the PNG bytes."""
    if not enabled():
        raise PortraitError("Portrait generation is off. Set COMFYUI_URL to your ComfyUI server.")
    client_id = secrets.token_hex(8)
    try:
        r = await client.post(f"{COMFYUI_URL}/prompt",
                              json={"prompt": build_workflow(prompt, seed, width, height), "client_id": client_id}, timeout=30)
    except httpx.RequestError as e:
        raise PortraitError(f"Can't reach ComfyUI at {COMFYUI_URL}: {e}") from e
    if r.status_code != 200:
        raise PortraitError(f"ComfyUI rejected the workflow ({r.status_code}): {r.text[:300]}")
    prompt_id = r.json().get("prompt_id")
    if not prompt_id:
        raise PortraitError("ComfyUI did not return a prompt id.")

    deadline = asyncio.get_event_loop().time() + PORTRAIT_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.5)
        h = await client.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=30)
        entry = h.json().get(prompt_id) if h.status_code == 200 else None
        if not entry:
            continue
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
            detail = msgs[0][1].get("exception_message", "") if msgs else ""
            raise PortraitError(f"ComfyUI failed to generate the portrait. {detail}".strip())
        for out in (entry.get("outputs") or {}).values():
            for img in out.get("images", []):
                v = await client.get(f"{COMFYUI_URL}/view", params={
                    "filename": img["filename"], "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output")}, timeout=60)
                if v.status_code == 200:
                    return v.content
        if status.get("completed"):
            raise PortraitError("ComfyUI finished but returned no image.")
    raise PortraitError("Timed out waiting for ComfyUI.")


def save(character_id: int, png: bytes, kind: str = "character", ext: str = "png") -> str:
    """Store a portrait (or a scene picture, kind="scene") and return the URL path the web app uses."""
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{kind}-{character_id}-{secrets.token_hex(4)}.{ext}"
    (PORTRAIT_DIR / name).write_bytes(png)
    return f"/portraits/{name}"
