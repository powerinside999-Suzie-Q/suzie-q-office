# app/utils.py
import os
import httpx
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ----- Env -----
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BRAIN_URL = os.getenv("BRAIN_URL", "https://suzie-q-brain.onrender.com/analyze")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "America/Los_Angeles")

# Embedding model must match your Supabase vector dims:
# - text-embedding-3-large => 3072 dims
# - text-embedding-3-small => 1536 dims
EMBED_MODEL = "text-embedding-3-large"

HEADERS_SB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

def now_utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

# ---------- OpenAI helpers ----------
async def embed_text(text: str) -> List[float]:
    """
    Return embedding vector for given text using OpenAI embeddings endpoint.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")
    async with httpx.AsyncClient(timeout=60, headers={
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }) as client:
        r = await client.post("https://api.openai.com/v1/embeddings", json={
            "input": text,
            "model": EMBED_MODEL,
        })
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]

async def importance_score(text: str) -> int:
    """
    Ask OpenAI (chat) to rate importance 1..5.
    If the API fails or key missing, returns a safe default (2).
    """
    if not OPENAI_API_KEY:
        return 2
    prompt = (
        "Rate the business importance of the following note on a 1-5 integer scale. "
        "1=trivial, 3=useful, 5=critical for CEO memory.\n"
        f"Note: {text}\n"
        "Return ONLY the integer."
    )
    try:
        async with httpx.AsyncClient(timeout=40, headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            })
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
        # Extract first integer found
        digits = "".join(ch for ch in content if ch.isdigit())
        n = int(digits) if digits else 2
        return max(1, min(5, n))
    except Exception:
        return 2

async def call_brain(context: str) -> str:
    """
    Call your Suzie Q 'brain' service with provided context.
    Expects JSON with {"decision": "..."}.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(BRAIN_URL, json={"context": context})
        r.raise_for_status()
        data = r.json()
        return data.get("decision") or data.get("body", {}).get("decision") or "No decision."

# ---------- Supabase helpers ----------
async def supabase_insert(table: str, payload: Dict[str, Any]) -> None:
    if not SUPABASE_URL:
        return
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        await client.post(f"{SUPABASE_URL}/rest/v1/{table}", json=payload)

async def supabase_select(table: str, query: str = "select=*") -> List[Dict[str, Any]]:
    if not SUPABASE_URL:
        return []
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}?{query}")
        r.raise_for_status()
        return r.json()

async def supabase_rpc(function: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not SUPABASE_URL:
        return []
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{function}", json=payload)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return [data]

async def sb_get_one(table: str, filter_qs: str) -> Optional[Dict[str, Any]]:
    """
    Return first matching row or None.
    Example filter_qs: 'select=*&name=eq.Sales'
    """
    if not SUPABASE_URL:
        return None
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}?{filter_qs}")
        r.raise_for_status()
        arr = r.json()
        return arr[0] if arr else None

async def sb_insert_returning(table: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Insert a row and return the created row (requires Prefer: return=representation).
    """
    if not SUPABASE_URL:
        return None
    headers = dict(HEADERS_SB)
    headers["Prefer"] = "return=representation"
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/{table}", json=payload)
        r.raise_for_status()
        arr = r.json()
        return arr[0] if arr else None

def agent_endpoint(dept: str, role: str, name: str) -> str:
    """
    Build public agent URL using PUBLIC_BASE_URL.
    """
    def enc(s: str) -> str:
        return urllib.parse.quote(s, safe="")
    base = PUBLIC_BASE_URL or ""
    return f"{base}/agents/{enc(dept)}/{enc(role)}/{enc(name)}"

# ---------- Slack / Telegram helpers ----------
async def slack_post_message(channel: str, text: str, thread_ts: Optional[str] = None) -> None:
    if not SLACK_BOT_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json;charset=utf-8",
    }
    body: Dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        await client.post("https://slack.com/api/chat.postMessage", json=body)

async def telegram_send_message(chat_id: int, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )

        )

