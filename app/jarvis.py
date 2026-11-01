"""
Jarvis: Dreamnet.ai's operations chief of staff, at /jarvis.

A Python port of the shared Jarvis engine used in every Joslin Enterprises repo
(jarvis-core.js): it reads Stripe and GitHub (GET requests only, it never writes)
and has Claude write a briefing and answer questions. Same API and page as the
other companies, so the Joslin Enterprises master Jarvis treats it the same way.

Environment variables:
  JARVIS_ACCESS_CODE  required; Jarvis stays locked until it is set
  ANTHROPIC_API_KEY   Claude, for briefings and questions
  JARVIS_STRIPE_KEY   a restricted, read-only Stripe key (falls back to STRIPE_SECRET_KEY)
  GITHUB_TOKEN        a fine-grained, read-only token for this repo
  JARVIS_GITHUB_REPO  optional owner/repo (default WinstonAIButler/dreamnet.ai)
  JARVIS_MODEL        optional, default claude-sonnet-5-5
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

PAGE = Path(__file__).resolve().parent / "static" / "jarvis.html"
STRIPE = "https://api.stripe.com/v1"
GITHUB = "https://api.github.com"
ANTHROPIC = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
DAY = 86400

COMPANY = {
    "id": "dreamnet",
    "name": "Dreamnet.ai",
    "repo": "WinstonAIButler/dreamnet.ai",
    "context": (
        "Dreamnet.ai is a writing studio for dark romance novels: an AI co-author with a story bible "
        "per novel, a character library and creator, and character roleplay, running on open-weight "
        "models through Featherless.ai. It is a FastAPI app hosted on Render."
    ),
}

# Tests swap in an httpx.MockTransport here.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class JarvisError(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=TRANSPORT, timeout=60)


def config(env=None) -> dict:
    env = os.environ if env is None else env
    return {
        "access_code": env.get("JARVIS_ACCESS_CODE", ""),
        "anthropic_key": env.get("ANTHROPIC_API_KEY", ""),
        "model": env.get("JARVIS_MODEL") or DEFAULT_MODEL,
        "github_token": env.get("GITHUB_TOKEN") or env.get("JARVIS_GITHUB_TOKEN", ""),
        "repo": env.get("JARVIS_GITHUB_REPO") or COMPANY["repo"],
        "stripe_key": env.get("JARVIS_STRIPE_KEY") or env.get("STRIPE_SECRET_KEY", ""),
    }


def _same_secret(a: str, b: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


# --- Stripe (read-only) -----------------------------------------------------

async def _stripe_get(client, key, path, params=None):
    r = await client.get(f"{STRIPE}{path}", params=params or {},
                         headers={"Authorization": f"Bearer {key}", "Stripe-Version": "2024-06-20"})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        raise JarvisError((data.get("error") or {}).get("message") or f"Stripe {r.status_code}")
    return data


async def _stripe_list(client, key, path, params, max_pages):
    items, after = [], None
    for _ in range(max_pages):
        page = await _stripe_get(client, key, path, {**params, "limit": "100", **({"starting_after": after} if after else {})})
        items.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("data"):
            return items, True
        after = page["data"][-1]["id"]
    return items, False


def _add(m, currency, cents):
    cur = str(currency or "usd").upper()
    m[cur] = m.get(cur, 0) + cents


def _monthly_cents(item):
    price = item.get("price") or item.get("plan") or {}
    amount = (price.get("unit_amount") if price.get("unit_amount") is not None else price.get("amount") or 0) * (item.get("quantity") or 1)
    rec = price.get("recurring") or {"interval": price.get("interval"), "interval_count": price.get("interval_count")}
    n = rec.get("interval_count") or 1
    interval = rec.get("interval")
    if interval == "day":
        return amount * 365 / 12 / n
    if interval == "week":
        return amount * 52 / 12 / n
    if interval == "year":
        return amount / 12 / n
    return amount / n


def _dollars(m):
    return {k: round(v) / 100 for k, v in m.items()}


async def stripe_status(key, now=None):
    if not key:
        return {"ok": False, "error": "No Stripe key set."}
    if not re.match(r"^(sk|rk)_", key):
        return {"ok": False, "error": "That isn't a Stripe secret or restricted key."}
    now = now or time.time()
    since = int(now) - 30 * DAY
    try:
        async with _client() as c:
            balance, (charges, complete), (subs, _), invoices = await asyncio.gather(
                _stripe_get(c, key, "/balance"),
                _stripe_list(c, key, "/charges", {"created[gte]": str(since)}, 5),
                _stripe_list(c, key, "/subscriptions", {"status": "active"}, 3),
                _stripe_get(c, key, "/invoices", {"status": "open", "limit": "20"}),
            )
    except (JarvisError, httpx.HTTPError) as e:
        return {"ok": False, "error": str(e)[:200]}
    available, pending, revenue, refunds, mrr, due = {}, {}, {}, {}, {}, {}
    for b in balance.get("available", []):
        _add(available, b.get("currency"), b.get("amount", 0))
    for b in balance.get("pending", []):
        _add(pending, b.get("currency"), b.get("amount", 0))
    count = 0
    for ch in charges:
        if ch.get("status") != "succeeded" or not ch.get("paid"):
            continue
        count += 1
        _add(revenue, ch.get("currency"), ch.get("amount", 0))
        if ch.get("amount_refunded"):
            _add(refunds, ch.get("currency"), ch["amount_refunded"])
    for s in subs:
        for it in (s.get("items") or {}).get("data", []):
            _add(mrr, (it.get("price") or {}).get("currency") or s.get("currency"), _monthly_cents(it))
    overdue, listed = 0, []
    for inv in invoices.get("data", []):
        _add(due, inv.get("currency"), inv.get("amount_remaining") or 0)
        is_overdue = bool(inv.get("due_date") and inv["due_date"] < int(now))
        overdue += is_overdue
        listed.append({
            "customer": inv.get("customer_name") or inv.get("customer_email") or "Customer",
            "amount": (inv.get("amount_remaining") or 0) / 100,
            "currency": str(inv.get("currency") or "usd").upper(),
            "dueDate": datetime.fromtimestamp(inv["due_date"], timezone.utc).date().isoformat() if inv.get("due_date") else None,
            "overdue": is_overdue,
        })
    return {
        "ok": True,
        "mode": "test" if "_test_" in key else "live",
        "balanceAvailable": _dollars(available),
        "balancePending": _dollars(pending),
        "revenue30d": _dollars(revenue),
        "refunds30d": _dollars(refunds),
        "charges30d": count,
        "revenueComplete": complete,
        "activeSubscriptions": len(subs),
        "mrr": _dollars(mrr),
        "openInvoices": {"count": len(listed), "overdue": overdue, "due": _dollars(due), "items": listed[:8]},
    }


# --- GitHub (read-only) -----------------------------------------------------

async def _gh_get(client, token, path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "jarvis-ops", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await client.get(f"{GITHUB}{path}", headers=headers)
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        hint = " (private repo? set GITHUB_TOKEN)" if r.status_code == 404 and not token else ""
        msg = data.get("message") if isinstance(data, dict) else None
        raise JarvisError(f"{msg or 'GitHub ' + str(r.status_code)}{hint}")
    return data


async def github_status(repo, token):
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return {"ok": False, "error": "No GitHub repo configured."}
    try:
        async with _client() as c:
            async def deployments():
                try:
                    return await _gh_get(c, token, f"/repos/{repo}/deployments?per_page=3")
                except JarvisError:
                    return []
            pulls, commits, deps = await asyncio.gather(
                _gh_get(c, token, f"/repos/{repo}/pulls?state=open&per_page=10"),
                _gh_get(c, token, f"/repos/{repo}/commits?per_page=5"),
                deployments(),
            )

            async def deploy(d):
                try:
                    st = await _gh_get(c, token, f"/repos/{repo}/deployments/{d['id']}/statuses?per_page=1")
                except JarvisError:
                    st = []
                first = st[0] if st else {}
                return {"environment": d.get("environment"), "state": first.get("state") or "unknown",
                        "at": first.get("created_at") or d.get("created_at"),
                        "url": first.get("environment_url") or first.get("target_url")}
            deploys = await asyncio.gather(*(deploy(d) for d in deps or []))
    except (JarvisError, httpx.HTTPError) as e:
        return {"ok": False, "repo": repo, "error": str(e)[:200]}
    return {
        "ok": True,
        "repo": repo,
        "openPullRequests": [{"number": p["number"], "title": p.get("title"), "author": (p.get("user") or {}).get("login"),
                              "draft": bool(p.get("draft")), "openedAt": p.get("created_at"), "url": p.get("html_url")} for p in pulls],
        "recentCommits": [{"sha": cm["sha"][:7], "message": str((cm.get("commit") or {}).get("message") or "").split("\n")[0][:120],
                           "at": ((cm.get("commit") or {}).get("author") or {}).get("date")} for cm in commits],
        "deployments": list(deploys),
    }


async def gather_status(cfg):
    stripe, github = await asyncio.gather(stripe_status(cfg["stripe_key"]), github_status(cfg["repo"], cfg["github_token"]))
    return {"mode": "company", "generatedAt": datetime.now(timezone.utc).isoformat(),
            "id": COMPANY["id"], "name": COMPANY["name"], "stripe": stripe, "github": github}


# --- Claude -----------------------------------------------------------------

def _system(status):
    return "\n".join([
        f"You are Jarvis, the operations chief of staff for {COMPANY['name']}. {COMPANY['context']}",
        "",
        "You work from the live snapshot below (Stripe and GitHub, read moments ago). Rules:",
        "- Use only numbers that appear in the snapshot. Never invent revenue, customers, or deals.",
        "- If a source has ok:false, say it isn't connected and what to set up; don't guess around it.",
        "- Stripe 'test' mode means test data: say so before quoting any figure from it.",
        "- You can read but not act: you cannot send payments, merge code, email anyone, or change anything. Never say you did.",
        "- Sales and marketing advice must be concrete and specific to this business: a named action, who it's for, and why now.",
        "- No investment, tax, or legal advice; point to a professional for those.",
        "- Plain, direct language. No hype.",
        "",
        "Snapshot (JSON):",
        json.dumps(status, separators=(",", ":")),
    ])


BRIEFING_ASK = """Write today's briefing in Markdown, under 300 words, with exactly these sections:
## Top 3 priorities today
## Revenue
## Dev status
## Sales & marketing moves
Priorities should be ranked by impact. Under Sales & marketing, give 2 or 3 specific actions."""


async def _claude(cfg, system, messages, max_tokens):
    if not cfg["anthropic_key"]:
        raise JarvisError("ANTHROPIC_API_KEY isn't set.", 503)
    async with _client() as c:
        r = await c.post(ANTHROPIC, headers={"x-api-key": cfg["anthropic_key"], "anthropic-version": "2023-06-01",
                                             "content-type": "application/json"},
                         json={"model": cfg["model"], "max_tokens": max_tokens, "system": system, "messages": messages})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        status = 429 if r.status_code == 429 else 500 if r.status_code == 401 else 502
        msg = "The Anthropic API key was rejected." if r.status_code == 401 else (data.get("error") or {}).get("message") or f"Claude {r.status_code}"
        raise JarvisError(msg, status)
    return "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _clean(messages):
    if not isinstance(messages, list):
        return []
    out = [{"role": m["role"], "content": m["content"][:4000]} for m in messages
           if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()][-12:]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


# --- Routes -----------------------------------------------------------------

def _json(body, status=200):
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/jarvis")
async def jarvis_page():
    return FileResponse(PAGE, media_type="text/html", headers={"X-Robots-Tag": "noindex, nofollow"})


@router.get("/api/jarvis")
async def jarvis_setup():
    cfg = config()
    return _json({
        "mode": "company", "name": COMPANY["name"], "accessCodeSet": bool(cfg["access_code"]),
        "claude": bool(cfg["anthropic_key"]), "githubToken": bool(cfg["github_token"]),
        "companies": [{"id": COMPANY["id"], "name": COMPANY["name"], "stripe": bool(cfg["stripe_key"]), "repo": cfg["repo"]}],
    })


@router.post("/api/jarvis")
async def jarvis_api(request: Request):
    cfg = config()
    if not cfg["access_code"]:
        return _json({"error": "Jarvis is locked until JARVIS_ACCESS_CODE is set in the hosting environment."}, 503)
    given = request.headers.get("x-jarvis-code", "")
    if not given or not _same_secret(given, cfg["access_code"]):
        return _json({"error": "Wrong or missing access code."}, 401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = body.get("action") or "status"
    if action not in ("status", "brief", "ask"):
        return _json({"error": "Unknown action."}, 400)
    try:
        status = await gather_status(cfg)
        if action == "status":
            return _json({"status": status})
        if action == "brief":
            return _json({"status": status, "briefing": await _claude(cfg, _system(status), [{"role": "user", "content": BRIEFING_ASK}], 1600)})
        msgs = _clean(body.get("messages"))
        if not msgs or msgs[-1]["role"] != "user":
            return _json({"error": "Send at least one question."}, 400)
        return _json({"status": status, "reply": await _claude(cfg, _system(status), msgs, 1400)})
    except JarvisError as e:
        return _json({"error": str(e)[:300]}, e.status)
    except httpx.HTTPError:
        return _json({"error": "Jarvis couldn't reach Claude. Try again."}, 502)
