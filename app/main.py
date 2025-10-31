
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.schemas import SlackEvent, TelegramUpdate, AgentInvokePayload
from app.utils import call_brain, supabase_insert, supabase_select, slack_post_message, telegram_send_message, now_utc_iso

CEO_CHANNEL = os.getenv("CEO_SLACK_CHANNEL_ID", "")

app = FastAPI(title="Suzie Q – FastAPI OS")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/slack/events")
async def slack_events(req: Request):
    body = await req.json()
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    ev = SlackEvent(**body)
    event = ev.event or {}
    if event.get("bot_id"):
        return {"ok": True}

    text = event.get("text", "")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    decision = await call_brain(f"You are Suzie Q (CEO). Respond concisely. Input: {text}")
    await slack_post_message(channel, decision, thread_ts=thread_ts)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": "slack",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True}

@app.post("/telegram/webhook")
async def telegram_webhook(update: TelegramUpdate):
    msg = update.message or {}
    chat = msg.get("chat", {}) or {}
    text = msg.get("text") or ""
    chat_id = chat.get("id")

    decision = await call_brain(f"You are Suzie Q (CEO). Respond concisely. Input: {text}")
    if chat_id:
        await telegram_send_message(chat_id, decision)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": "telegram",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True}

@app.post("/agents/{dept}/{role}/{name}")
async def agent_invoke(dept: str, role: str, name: str, payload: AgentInvokePayload):
    text = (payload.text or payload.context) or ""
    prompt = f"You are an AI {role} for the {dept} department named {name}. Be specialized and concise. Input: {text}"
    decision = await call_brain(prompt)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": f"{dept}:{role}:{name}",
        "timestamp": now_utc_iso(),
        "department": dept,
        "actor": name
    })
    return {"agent": name, "role": role, "dept": dept, "decision": decision}

@app.post("/cron/daily-report")
async def daily_report():
    records = await supabase_select("memory", "select=*&order=timestamp.desc&limit=200")
    context = "Summarize the last 24 hours of Suzie Q operations into an executive report with KPIs and next actions.\n"
    for r in records or []:
        c = r.get("context", "")
        d = r.get("decision", "")
        context += f"- Context: {c}\n  Decision: {d}\n"
    decision = await call_brain(context or "Summarize recent activity.")
    if CEO_CHANNEL:
        await slack_post_message(CEO_CHANNEL, f"Daily CEO Report:\n{decision}")
    await supabase_insert("memory", {
        "context": "[system] daily-report",
        "decision": decision,
        "source": "cron",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True, "summary": decision}
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from app.schemas import SlackEvent, TelegramUpdate, AgentInvokePayload, RememberPayload, RecallPayload
from app.utils import (
    call_brain, supabase_insert, supabase_rpc, slack_post_message, telegram_send_message,
    now_utc_iso, embed_text
)

app = FastAPI(title="Suzie Q – FastAPI OS")

# --- Memory: Remember ---
@app.post("/memory/remember")
async def remember(payload: RememberPayload):
    emb = await embed_text(payload.content)
    row = {
        "content": payload.content,
        "embedding": emb,
        "tags": payload.tags or [],
        "importance": payload.importance or 1,
        "source": payload.source or "api",
        "department": payload.department,
        "actor": payload.actor,
        "created_at": now_utc_iso(),
    }
    await supabase_insert("long_term_memory", row)
    return {"ok": True}

# --- Memory: Recall ---
@app.post("/memory/recall")
async def recall(payload: RecallPayload):
    emb = await embed_text(payload.query)
    matches = await supabase_rpc("match_long_term_memory", {
        "query_embedding": emb,
        "match_count": payload.top_k,
        "min_cosine_similarity": payload.min_similarity,
        "dept": payload.department,
    })
    return {"ok": True, "matches": matches}
