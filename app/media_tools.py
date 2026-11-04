"""
Dreamnet - make new pictures and videos from a gallery picture
- Remix: a new picture that starts from an existing one (same person, new outfit, place or pose). Online,
  the picture is handed to an image-editing model by a short-lived signed link; without a public address
  (or if that model fails) it falls back to the picture's own prompt plus the changes, on a new seed.
- Video: a short clip animated from a picture: on your own ComfyUI or cloud GPU with Wan 2.2 (see
  docs/cloud-gpu.md), or through Replicate's image-to-video models with REPLICATE_API_TOKEN.
  VIDEO_BACKEND picks one: auto (the cloud GPU when set up, else Replicate, else your ComfyUI), comfyui
  or replicate.
Only the picture and its prompt are sent, never chats.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import random
import secrets
import time
from urllib.parse import quote

import httpx

import app.gpu as gpu
import app.heat as heat

# Public address of this site, so an online model can fetch a picture through a signed link.
# Render sets RENDER_EXTERNAL_URL itself.
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
_SECRET = (os.getenv("SHARE_SECRET") or secrets.token_hex(32)).encode()
SHARE_SECONDS = 900

REMIX_SERVICE = os.getenv("REMIX_SERVICE", "https://image.pollinations.ai/prompt/{prompt}?model=kontext&image={image}"
                          "&width={width}&height={height}&seed={seed}&nologo=true&private=true&safe=true")
REMIX_TIMEOUT = float(os.getenv("REMIX_TIMEOUT", "180"))

REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
REPLICATE_API = os.getenv("REPLICATE_API", "https://api.replicate.com/v1").rstrip("/")
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "wan-video/wan-2.2-i2v-fast")
VIDEO_TIMEOUT = float(os.getenv("VIDEO_TIMEOUT", "600"))

VIDEO_BACKEND = os.getenv("VIDEO_BACKEND", "auto").strip().lower()

# Wan 2.2 TI2V 5B on ComfyUI (the files from Comfy-Org/Wan_2.2_ComfyUI_Repackaged).
WAN_UNET = os.getenv("WAN_UNET", "wan2.2_ti2v_5B_fp16.safetensors")
WAN_CLIP = os.getenv("WAN_CLIP", "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
WAN_VAE = os.getenv("WAN_VAE", "wan2.2_vae.safetensors")
VIDEO_FRAMES = int(os.getenv("VIDEO_FRAMES", "121"))   # 121 frames at 24 fps = about 5 seconds
VIDEO_STEPS = int(os.getenv("VIDEO_STEPS", "20"))
WAN_NEGATIVE = ("blurry, low quality, distorted face, deformed hands, extra limbs, flicker, static frame, "
                "cartoon, anime, CGI, illustration, watermark, text, subtitles")
# The 5B model is trained at 1280x704; each shape keeps about that many pixels.
VIDEO_SIZES = {"portrait": (704, 1280), "square": (960, 960), "landscape": (1280, 704), "wide": (1280, 704)}

VIDEO_LOOK = "Realistic live-action footage, natural motion, real skin and light, cinematic, steady camera."
VIDEO_STYLE = "Not animation, not cartoon, not CGI."


class MediaError(Exception):
    pass


# --- signed links for /portraits files ---

def _sig(path: str, exp: int) -> str:
    return hmac.new(_SECRET, f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_link(path: str) -> str:
    """Absolute link to a /portraits file that works without signing in, for SHARE_SECONDS."""
    exp = int(time.time()) + SHARE_SECONDS
    return f"{PUBLIC_URL}{path}?exp={exp}&sig={_sig(path, exp)}"


def check_link(path: str, exp: str, sig: str) -> bool:
    try:
        e = int(exp)
    except (TypeError, ValueError):
        return False
    return e >= time.time() and hmac.compare_digest(_sig(path, e), sig or "")


def remix_online_enabled() -> bool:
    return bool(PUBLIC_URL) and REMIX_SERVICE.strip().lower() not in ("", "off", "none")


def remix_prompt(original: str, changes: str) -> str:
    """What the editing model is told: keep the person, apply the changes, stay a realistic photo."""
    return (f"Keep exactly the same person: same face, hair, eyes, skin and body. {changes.strip().rstrip('.')}. "
            "An adult. Photorealistic photograph, natural skin texture, real light, cinematic colour grade. "
            "Not a drawing, not anime, not CGI. " + heat.rule(online=True))


def variation_prompt(original: str, changes: str) -> str:
    """Fallback when the picture itself can't be sent: its own prompt with the changes on top."""
    return f"{original.strip()} Changes for this version, which take priority: {changes.strip().rstrip('.')}."


async def remix_online(client: httpx.AsyncClient, image_path: str, prompt: str, width: int, height: int) -> bytes:
    url = REMIX_SERVICE.format(prompt=quote(prompt[:1500], safe=""), image=quote(signed_link(image_path), safe=""),
                               width=width, height=height, seed=random.randint(1, 2**31 - 1))
    try:
        r = await client.get(url, timeout=REMIX_TIMEOUT, follow_redirects=True)
    except Exception as e:
        raise MediaError(f"The image editing service didn't answer ({type(e).__name__}).") from e
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") or len(r.content) < 2000:
        raise MediaError(f"The image editing service couldn't make it ({r.status_code}).")
    return r.content


# --- video ---

def video_backend() -> str:
    """"comfyui", "replicate" or "" (videos off)."""
    if VIDEO_BACKEND == "comfyui":
        return "comfyui" if gpu.configured() else ""
    if VIDEO_BACKEND == "replicate":
        return "replicate" if REPLICATE_TOKEN else ""
    if gpu.runpod_enabled():
        return "comfyui"
    if REPLICATE_TOKEN:
        return "replicate"
    return "comfyui" if gpu.configured() else ""


def video_enabled() -> bool:
    return bool(video_backend())


def video_rules(backend: str) -> str:
    return f"{VIDEO_LOOK} {heat.rule(online=backend != 'comfyui')} {VIDEO_STYLE}"


def wan_workflow(image_name: str, prompt: str, shape: str, seed: int | None = None) -> dict:
    w, h = VIDEO_SIZES.get(shape, VIDEO_SIZES["portrait"])
    return {
        "37": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN_UNET, "weight_dtype": "default"}},
        "38": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN_CLIP, "type": "wan", "device": "default"}},
        "39": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "48": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 8, "model": ["37", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["38", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": WAN_NEGATIVE, "clip": ["38", 0]}},
        "56": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "55": {"class_type": "Wan22ImageToVideoLatent",
               "inputs": {"vae": ["39", 0], "start_image": ["56", 0], "width": w, "height": h,
                          "length": VIDEO_FRAMES, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed if seed is not None else random.randint(0, 2**48), "steps": VIDEO_STEPS,
                         "cfg": 5, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1,
                         "model": ["48", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["55", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "57": {"class_type": "CreateVideo", "inputs": {"images": ["8", 0], "fps": 24}},
        "58": {"class_type": "SaveVideo",
               "inputs": {"video": ["57", 0], "filename_prefix": "dreamnet/video", "format": "mp4", "codec": "h264"}},
    }


def _data_uri(data: bytes) -> str:
    kind = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else "image/png"
    return f"data:{kind};base64," + base64.b64encode(data).decode()


async def make_video(client: httpx.AsyncClient, image: bytes, motion: str, shape: str = "portrait") -> bytes:
    """Animate one picture into a short MP4 and return its bytes."""
    backend = video_backend()
    if not backend:
        raise MediaError("Videos need a cloud GPU (see docs/cloud-gpu.md) or a Replicate account "
                         "(REPLICATE_API_TOKEN).")
    prompt = f"{motion.strip().rstrip('.')}. {video_rules(backend)}"
    if backend == "comfyui":
        try:
            name = await gpu.upload_image(client, image)
            return await gpu.run_workflow(client, wan_workflow(name, prompt, shape), VIDEO_TIMEOUT,
                                          what="video", want=(".mp4", ".webm"))
        except gpu.GPUError as e:
            raise MediaError(str(e)) from e
    return await _replicate_video(client, image, prompt)


async def _replicate_video(client: httpx.AsyncClient, image: bytes, prompt: str) -> bytes:
    """Animate one picture into a short MP4 on Replicate and return its bytes."""
    headers = {"Authorization": f"Bearer {REPLICATE_TOKEN}", "Prefer": "wait=60"}
    body = {"input": {"image": _data_uri(image), "prompt": prompt}}
    try:
        r = await client.post(f"{REPLICATE_API}/models/{VIDEO_MODEL}/predictions", json=body, headers=headers, timeout=90)
    except Exception as e:
        raise MediaError(f"Replicate didn't answer ({type(e).__name__}). Try again.") from e
    if r.status_code in (401, 403):
        raise MediaError("Replicate refused the key. Check REPLICATE_API_TOKEN on the server.")
    if r.status_code == 402:
        raise MediaError("Replicate needs billing set up on your account before it makes videos.")
    if r.status_code >= 400:
        raise MediaError(f"Replicate couldn't start the video ({r.status_code}): {r.text[:200]}")
    pred = r.json()
    deadline = time.monotonic() + VIDEO_TIMEOUT
    while pred.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() > deadline:
            raise MediaError("The video took too long. Try again.")
        await asyncio.sleep(4)
        g = await client.get(pred["urls"]["get"], headers={"Authorization": f"Bearer {REPLICATE_TOKEN}"}, timeout=30)
        pred = g.json()
    if pred["status"] != "succeeded":
        raise MediaError(f"The video failed: {str(pred.get('error') or pred['status'])[:200]}")
    out = pred.get("output")
    link = out[0] if isinstance(out, list) else out
    if not isinstance(link, str):
        raise MediaError("Replicate finished but returned no video.")
    v = await client.get(link, timeout=120, follow_redirects=True)
    if v.status_code != 200 or len(v.content) < 1000:
        raise MediaError("Couldn't download the finished video.")
    return v.content
