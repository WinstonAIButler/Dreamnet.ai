"""
Dreamnet - launch foundations
Self-serve sign-up with an 18+ confirmation and terms acceptance, plus the public
pages a live product needs: pricing, trust, terms, privacy and content policy.

Beta settings (environment variables):
  SIGNUP_OPEN        "1" lets anyone create an account (default), "0" closes sign-up.
  SIGNUP_CREDITS     starting credits for a new account (default 5).
  DREAMNET_COMPANY   legal name shown on the legal pages.
  DREAMNET_CONTACT   support / legal email shown on the legal pages.

Age check: a click-through confirmation for the invite beta. Swap in a vendor
check (face-age estimation or ID) before the public launch and before serving
users in the UK or any US state with an age-verification law.
"""
import hashlib
import os
import re
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import create_session, hash_password
from app.database import create_user, get_user_by_username

DB_PATH = "proxy.db"  # re-pointed by main.py
HOSTED = os.getenv("DREAMNET_HOSTED", "") == "1"
SIGNUP_OPEN = os.getenv("SIGNUP_OPEN", "1") == "1"
SIGNUP_CREDITS = float(os.getenv("SIGNUP_CREDITS", "5"))
COMPANY = os.getenv("DREAMNET_COMPANY", "Dreamnet.ai")
CONTACT = os.getenv("DREAMNET_CONTACT", "")
TERMS_VERSION = "2026-11-10"
UPDATED = "November 10, 2026"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
RESERVED = {"admin", "administrator", "root", "support", "dreamnet", "moderator", "staff", "system", "jarvis"}

# Sign-ups per network address, so one person can't mint endless free accounts.
SIGNUP_MAX_PER_HOUR = int(os.getenv("SIGNUP_MAX_PER_HOUR", "3"))
_signups: dict[str, list[float]] = {}

# Launch pricing. Checkout isn't connected yet (an adult-friendly processor is still
# to be chosen), so the pricing page shows these as "opening soon".
PLANS = [
    {"name": "Free", "price": "$0", "per": "", "points": [
        "Unlimited chat with every character", "Long-term memory", "Create your own characters",
        "Free voices that play on your device"], "cta": "Create free account", "best": False},
    {"name": "Premium", "price": "$14.99", "per": "/month", "points": [
        "Everything in Free", "Every chat model, including the smartest ones",
        "1,500 credits a month that roll over", "HD pictures and voice calls", "Cancel any time in one tap"],
        "cta": "Opening soon", "best": True},
    {"name": "Premium yearly", "price": "$99", "per": "/year", "points": [
        "Everything in Premium", "Works out to $8.25 a month", "2,000 bonus credits up front"],
        "cta": "Opening soon", "best": False},
]

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


async def migrate(db_path: str):
    """Record when each account confirmed its age and accepted the terms."""
    async with aiosqlite.connect(db_path) as db:
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        for col, kind in (("age_confirmed_at", "REAL"), ("terms_accepted_at", "REAL"),
                          ("terms_version", "TEXT"), ("signup_ip_hash", "TEXT")):
            if col not in cols:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {kind}")
        await db.commit()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "unknown")


def _ip_hash(ip: str) -> str:
    # Stored hashed: enough to spot abuse, without keeping people's addresses.
    return hashlib.sha256(("dreamnet-signup:" + ip).encode()).hexdigest()[:32]


def _recent_signups(ip: str) -> list[float]:
    cutoff = time.time() - 3600
    recent = [t for t in _signups.get(ip, []) if t > cutoff]
    if recent:
        _signups[ip] = recent
    else:
        _signups.pop(ip, None)
    return recent


@router.get("/api/signup-status")
async def signup_status():
    return {"open": SIGNUP_OPEN, "age_check": "click-through", "terms_version": TERMS_VERSION}


@router.post("/api/signup")
async def signup(request: Request):
    if not SIGNUP_OPEN:
        return JSONResponse({"error": "Sign-up is closed right now. Ask for an invite."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Send the sign-up form as JSON."}, status_code=400)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not USERNAME_RE.match(username):
        return JSONResponse({"error": "Pick a username of 3 to 24 letters, numbers or underscores."}, status_code=400)
    if username.lower() in RESERVED:
        return JSONResponse({"error": "That username is reserved. Pick another."}, status_code=400)
    if len(password) < 8 or len(password) > 128:
        return JSONResponse({"error": "Use a password of at least 8 characters."}, status_code=400)
    if body.get("age_confirmed") is not True:
        return JSONResponse({"error": "Dreamnet is for adults. Confirm you're 18 or older to continue."}, status_code=400)
    if body.get("terms_accepted") is not True:
        return JSONResponse({"error": "Accept the Terms and Content Policy to continue."}, status_code=400)
    ip = _client_ip(request)
    if len(_recent_signups(ip)) >= SIGNUP_MAX_PER_HOUR:
        return JSONResponse({"error": "Too many new accounts from this network. Try again in an hour."}, status_code=429)
    if await get_user_by_username(DB_PATH, username):
        return JSONResponse({"error": "That username is taken. Pick another."}, status_code=409)
    async with aiosqlite.connect(DB_PATH) as db:  # also catches disabled accounts with that name
        row = await (await db.execute("SELECT 1 FROM users WHERE lower(username) = lower(?)", (username,))).fetchone()
    if row:
        return JSONResponse({"error": "That username is taken. Pick another."}, status_code=409)

    user = await create_user(DB_PATH, username, hash_password(password), is_admin=0,
                             credits=SIGNUP_CREDITS, credit_limit=0)
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET age_confirmed_at = ?, terms_accepted_at = ?, terms_version = ?, "
                         "signup_ip_hash = ? WHERE id = ?", (now, now, TERMS_VERSION, _ip_hash(ip), user["id"]))
        if SIGNUP_CREDITS:
            await db.execute("INSERT INTO credit_transactions (user_id, amount, reason, balance_after, timestamp) "
                             "VALUES (?,?,?,?,?)", (user["id"], SIGNUP_CREDITS, "signup_bonus", SIGNUP_CREDITS, now))
        await db.commit()
    _signups.setdefault(ip, []).append(now)

    token = await create_session(DB_PATH, user["id"], username, 0)
    resp = JSONResponse({"ok": True, "username": username})
    resp.set_cookie("session", token, httponly=True, max_age=86400, samesite="lax", secure=HOSTED)
    return resp


PAGES = {
    "pricing": "Pricing",
    "trust": "Trust & safety",
    "terms": "Terms of Service",
    "privacy": "Privacy Policy",
    "content-policy": "Content Policy",
}


def _page(name: str):
    async def handler(request: Request):
        return templates.TemplateResponse(request=request, name=f"public/{name}.html", context={
            "title": PAGES[name], "page": name, "company": COMPANY, "contact": CONTACT,
            "updated": UPDATED, "plans": PLANS, "signup_credits": SIGNUP_CREDITS})
    return handler


for _name in PAGES:
    router.add_api_route(f"/{_name}", _page(_name), methods=["GET"], response_class=HTMLResponse)
