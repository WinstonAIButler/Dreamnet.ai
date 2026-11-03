"""
Dreamnet - make new pictures and videos from a gallery picture
- Remix: a new picture that starts from an existing one (same person, new outfit, place or pose). Online,
  the picture is handed to an image-editing model by a short-lived signed link; without a public address
  (or if that model fails) it falls back to the picture's own prompt plus the changes, on a new seed.
- Video: a short clip animated from a picture, through Replicate's image-to-video models. It needs
  REPLICATE_API_TOKEN; without it the button explains what to set.
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

VIDEO_RULES = ("Realistic live-action footage, natural motion, real skin and light, cinematic, steady camera. "
               "Every person is an adult. Tasteful, no explicit nudity. Not animation, not cartoon, not CGI.")


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
            "Not a drawing, not anime, not CGI. No explicit nudity.")


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

def video_enabled() -> bool:
    return bool(REPLICATE_TOKEN)


def _data_uri(data: bytes) -> str:
    kind = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else "image/png"
    return f"data:{kind};base64," + base64.b64encode(data).decode()


async def make_video(client: httpx.AsyncClient, image: bytes, motion: str) -> bytes:
    """Animate one picture into a short MP4 on Replicate and return its bytes."""
    if not video_enabled():
        raise MediaError("Videos need a Replicate account: set REPLICATE_API_TOKEN on the server (see README).")
    headers = {"Authorization": f"Bearer {REPLICATE_TOKEN}", "Prefer": "wait=60"}
    body = {"input": {"image": _data_uri(image), "prompt": f"{motion.strip().rstrip('.')}. {VIDEO_RULES}"}}
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
