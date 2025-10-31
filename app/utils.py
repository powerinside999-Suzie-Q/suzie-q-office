
import os, httpx
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
BRAIN_URL = os.getenv("BRAIN_URL", "https://suzie-q-brain.onrender.com/analyze")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

HEADERS_SB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

async def call_brain(context: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(BRAIN_URL, json={"context": context})
        r.raise_for_status()
        data = r.json()
        return data.get("decision") or data.get("body", {}).get("decision") or "No decision."

async def supabase_insert(table: str, payload: dict):
    if not SUPABASE_URL:
        return
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        await client.post(f"{SUPABASE_URL}/rest/v1/{table}", json=payload)

async def supabase_select(table: str, query: str = "select=*"):
    if not SUPABASE_URL:
        return []
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{table}?{query}")
        r.raise_for_status()
        return r.json()

async def slack_post_message(channel: str, text: str, thread_ts: str | None = None):
    if not SLACK_BOT_TOKEN:
        return
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json;charset=utf-8"}
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        await client.post("https://slack.com/api/chat.postMessage", json=body)

async def telegram_send_message(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text})

def now_utc_iso():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
