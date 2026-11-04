"""
Dreamnet - the ComfyUI server that makes pictures and videos.

Either a ComfyUI you run yourself (COMFYUI_URL), or a RunPod GPU pod that Dreamnet switches on and off
for you (RUNPOD_API_KEY + RUNPOD_POD_ID). A pod is billed only while it runs, so Dreamnet stops it after
GPU_IDLE_MINUTES without use.

COMFYUI_USER / COMFYUI_PASSWORD: the basic-auth login in front of a ComfyUI that's reachable from the
internet (a RunPod pod's proxy address is public). See docs/cloud-gpu.md.
"""
import asyncio
import logging
import os
import secrets
import time

import httpx

logger = logging.getLogger("dreamnet.gpu")

COMFYUI_URL = os.getenv("COMFYUI_URL", "").rstrip("/")
COMFYUI_USER = os.getenv("COMFYUI_USER", "")
COMFYUI_PASSWORD = os.getenv("COMFYUI_PASSWORD", "")

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_POD_ID = os.getenv("RUNPOD_POD_ID", "").strip()
RUNPOD_PORT = int(os.getenv("RUNPOD_COMFY_PORT", "8188"))
RUNPOD_API = os.getenv("RUNPOD_API", "https://rest.runpod.io/v1").rstrip("/")
IDLE_MINUTES = float(os.getenv("GPU_IDLE_MINUTES", "15"))

_last_used = time.monotonic()
_started_by_us = False


class GPUError(Exception):
    pass


def runpod_enabled() -> bool:
    return bool(RUNPOD_API_KEY and RUNPOD_POD_ID)


def url() -> str:
    """Address of the ComfyUI server, or "" when none is set up."""
    if RUNPOD_POD_ID:
        return f"https://{RUNPOD_POD_ID}-{RUNPOD_PORT}.proxy.runpod.net"
    return COMFYUI_URL


def configured() -> bool:
    return bool(url())


def own_gpu() -> bool:
    """True when pictures are made on a ComfyUI the owner controls (not a public online service)."""
    return configured()


def auth() -> httpx.BasicAuth | None:
    return httpx.BasicAuth(COMFYUI_USER, COMFYUI_PASSWORD) if COMFYUI_USER else None


def touch():
    global _last_used
    _last_used = time.monotonic()


# --- RunPod pod control ---

def _rp_headers() -> dict:
    return {"Authorization": f"Bearer {RUNPOD_API_KEY}"}


async def pod_state(client: httpx.AsyncClient) -> dict:
    """{"status": "RUNNING" | "EXITED" | ..., "cost_per_hr": float | None}."""
    try:
        r = await client.get(f"{RUNPOD_API}/pods/{RUNPOD_POD_ID}", headers=_rp_headers(), timeout=20)
    except httpx.RequestError as e:
        raise GPUError(f"Can't reach RunPod ({type(e).__name__}).") from e
    if r.status_code in (401, 403):
        raise GPUError("RunPod refused the key. Check RUNPOD_API_KEY.")
    if r.status_code == 404:
        raise GPUError("RunPod has no pod with that id. Check RUNPOD_POD_ID.")
    if r.status_code >= 400:
        raise GPUError(f"RunPod answered {r.status_code}: {r.text[:200]}")
    p = r.json()
    return {"status": str(p.get("desiredStatus") or "UNKNOWN").upper(), "cost_per_hr": p.get("costPerHr")}


async def _pod_action(client: httpx.AsyncClient, action: str):
    try:
        r = await client.post(f"{RUNPOD_API}/pods/{RUNPOD_POD_ID}/{action}", headers=_rp_headers(), timeout=30)
    except httpx.RequestError as e:
        raise GPUError(f"Can't reach RunPod ({type(e).__name__}).") from e
    if r.status_code >= 400:
        raise GPUError(f"RunPod couldn't {action} the GPU ({r.status_code}): {r.text[:200]}")


async def start(client: httpx.AsyncClient):
    global _started_by_us
    if not runpod_enabled():
        raise GPUError("No cloud GPU is set up: set RUNPOD_API_KEY and RUNPOD_POD_ID (see docs/cloud-gpu.md).")
    await _pod_action(client, "start")
    _started_by_us = True
    touch()


async def stop(client: httpx.AsyncClient):
    if not runpod_enabled():
        raise GPUError("No cloud GPU is set up.")
    await _pod_action(client, "stop")


async def comfy_ready(client: httpx.AsyncClient) -> bool:
    if not configured():
        return False
    try:
        r = await client.get(f"{url()}/system_stats", auth=auth(), timeout=10)
        return r.status_code == 200
    except httpx.RequestError:
        return False


async def status(client: httpx.AsyncClient) -> dict:
    out = {"configured": configured(), "runpod": runpod_enabled(), "status": "", "ready": False,
           "cost_per_hr": None, "idle_minutes": IDLE_MINUTES, "problem": ""}
    if runpod_enabled():
        try:
            out.update(await pod_state(client))
        except GPUError as e:
            out["problem"] = str(e)
            return out
        if out["status"] != "RUNNING":
            return out
    out["ready"] = await comfy_ready(client)
    if out["ready"] and not out["status"]:
        out["status"] = "RUNNING"
    return out


async def idle_watch(client_getter):
    """Stop the RunPod pod after IDLE_MINUTES without a picture or video. Runs for the app's lifetime."""
    while True:
        await asyncio.sleep(60)
        if not runpod_enabled() or IDLE_MINUTES <= 0:
            continue
        if time.monotonic() - _last_used < IDLE_MINUTES * 60:
            continue
        client = client_getter()
        if client is None:
            continue
        try:
            if (await pod_state(client))["status"] == "RUNNING":
                await stop(client)
                logger.info("Stopped the cloud GPU after %s idle minutes", IDLE_MINUTES)
        except GPUError as e:
            logger.warning("Idle check on the cloud GPU failed: %s", e)
        touch()  # don't hammer RunPod: check again after another idle period


# --- running ComfyUI workflows ---

async def _not_ready_message(client: httpx.AsyncClient) -> str:
    if runpod_enabled():
        try:
            st = (await pod_state(client))["status"]
        except GPUError as e:
            return str(e)
        if st != "RUNNING":
            return "The cloud GPU is off. Press Start GPU on the Create image page, then try again in a minute or two."
        return "The cloud GPU is starting up. Try again in a minute."
    return f"Can't reach ComfyUI at {url()}."


async def upload_image(client: httpx.AsyncClient, data: bytes, name: str = "") -> str:
    """Put a picture into ComfyUI's input folder and return the name a LoadImage node uses."""
    kind = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else "image/png"
    name = name or f"dreamnet-{secrets.token_hex(6)}." + kind.split("/")[1]
    try:
        r = await client.post(f"{url()}/upload/image", files={"image": (name, data, kind)},
                              data={"overwrite": "true"}, auth=auth(), timeout=60)
    except httpx.RequestError as e:
        raise GPUError(await _not_ready_message(client)) from e
    if r.status_code != 200:
        raise GPUError(f"ComfyUI wouldn't take the picture ({r.status_code}).")
    j = r.json()
    return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]


async def run_workflow(client: httpx.AsyncClient, workflow: dict, timeout: float, what: str = "picture",
                       want: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")) -> bytes:
    """Queue a workflow, wait for it, and return the bytes of its first output file ending in one of `want`.
    The finished job is then removed from ComfyUI's history so prompts and results don't linger there."""
    if not configured():
        raise GPUError("Picture making is off. Set COMFYUI_URL, or set up a cloud GPU (see docs/cloud-gpu.md).")
    touch()
    base, a = url(), auth()
    client_id = secrets.token_hex(8)
    try:
        r = await client.post(f"{base}/prompt", json={"prompt": workflow, "client_id": client_id}, auth=a, timeout=30)
    except httpx.RequestError as e:
        raise GPUError(await _not_ready_message(client)) from e
    if r.status_code in (401, 403):
        raise GPUError("ComfyUI refused the login. Check COMFYUI_USER and COMFYUI_PASSWORD.")
    if r.status_code in (502, 503, 504, 404) and runpod_enabled():
        raise GPUError(await _not_ready_message(client))
    if r.status_code != 200:
        raise GPUError(f"ComfyUI rejected the workflow ({r.status_code}): {r.text[:300]}")
    prompt_id = r.json().get("prompt_id")
    if not prompt_id:
        raise GPUError("ComfyUI did not return a prompt id.")

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            touch()
            try:
                h = await client.get(f"{base}/history/{prompt_id}", auth=a, timeout=30)
            except httpx.RequestError:
                continue
            entry = h.json().get(prompt_id) if h.status_code == 200 else None
            if not entry:
                continue
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                detail = msgs[0][1].get("exception_message", "") if msgs else ""
                raise GPUError(f"ComfyUI failed to make the {what}. {detail}".strip())
            for out in (entry.get("outputs") or {}).values():
                for files in out.values():
                    if not isinstance(files, list):
                        continue
                    for f in files:
                        if not isinstance(f, dict) or not str(f.get("filename", "")).lower().endswith(want):
                            continue
                        v = await client.get(f"{base}/view", params={
                            "filename": f["filename"], "subfolder": f.get("subfolder", ""),
                            "type": f.get("type", "output")}, auth=a, timeout=180)
                        if v.status_code == 200:
                            return v.content
            if st.get("completed"):
                raise GPUError(f"ComfyUI finished but returned no {what}.")
        raise GPUError(f"Timed out waiting for ComfyUI to make the {what}.")
    finally:
        try:
            await client.post(f"{base}/history", json={"delete": [prompt_id]}, auth=a, timeout=15)
        except Exception:
            pass
