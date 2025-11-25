# app/main.py

import os
import json
import asyncio
import urllib.parse
from typing import Optional, List, Dict, Any
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

import base64

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

from app.schemas import (
    SlackEvent,
    AgentInvokePayload,
    RememberPayload,
    RecallPayload,
    StaffCreatePayload,
    StaffDeletePayload,
)
from app.utils import (
    call_brain,
    embed_text,
    importance_score,
    supabase_insert,
    supabase_select,
    supabase_rpc,
    slack_post_message,
    telegram_send_message,
    now_utc_iso,
    sb_get_one,
    sb_insert_returning,
    agent_endpoint,
    HEADERS_SB,
    SUPABASE_URL,
)

# --------------------------------
# ENV & HELPERS
# --------------------------------

CEO_CHANNEL = os.getenv("CEO_SLACK_CHANNEL_ID", "")  # optional CEO report channel
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")


def _enc(s: str) -> str:
    """URL-safe encoding for Supabase filters."""
    return urllib.parse.quote(s, safe="")


def _parts(text: str) -> List[str]:
    return text.strip().split() if text else []


async def _post_channel(channel_id: Optional[str], text: str, thread_ts: Optional[str] = None) -> None:
    """Helper to safely post to Slack (no-op if channel is missing)."""
    if channel_id:
        await slack_post_message(channel_id, text, thread_ts=thread_ts)


# --------------------------------
# FASTAPI APP
# --------------------------------

app = FastAPI(title="Suzie Q – Money Machine")


# --------------------------------
# ROOT & HEALTH
# --------------------------------

@app.get("/")
def root():
    return {"message": "Suzie Q is running and ready to make money."}


@app.get("/health")
def health():
    return {"ok": True}


# --------------------------------
# SLACK EVENTS
# --------------------------------

@app.get("/slack/events")
def slack_events_get():
    """So hitting this URL in a browser doesn't 405."""
    return PlainTextResponse("Slack Events endpoint. Use POST.", status_code=200)


@app.post("/slack/events")
async def slack_events(req: Request):
    """
    Handles Slack Event Subscriptions (POST JSON).
    - URL verification returns challenge
    - app_mention / message events → call brain → reply → log memory
    """
    body = await req.json()

    # Slack URL verification handshake
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    ev = SlackEvent(**body)
    event = ev.event or {}

    # Ignore the bot's own messages
    if event.get("bot_id"):
        return {"ok": True}

    text = event.get("text") or ""
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # Try to recall relevant memory first
    memory_snips = ""
    try:
        if text:
            q_emb = await embed_text(text)
            matches = await supabase_rpc("match_long_term_memory_ranked", {
                "query_embedding": q_emb,
                "match_count": 6,
                "dept": None,
                "min_cosine_similarity": 0.20,
                "half_life_days": 14.0,
                "alpha": 0.6,
                "beta": 0.3,
            }) or []
            memory_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        memory_snips = ""

    prefix = "You are Suzie Q, an AI CEO. Use relevant memory when helpful.\n"
    if memory_snips:
        prefix += f"Relevant memory:\n{memory_snips}\n\n"

    prompt = prefix + f"User: {text}"
    decision = await call_brain(prompt)

    # Post back to Slack
    if channel:
        await slack_post_message(channel, decision, thread_ts=thread_ts)

    # Log to short-term memory
    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": "slack",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True}


# --------------------------------
# SLACK: /hire – build departments & staff
# --------------------------------

@app.post("/slack/commands/hire")
async def slack_hire(req: Request):
    """
    /hire <department> [names...]
    Example:
      /hire Marketing AnalystA AnalystB Designer Copywriter MediaBuyer
    """
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}

    text = (data.get("text") or "").strip()
    user = data.get("user_name") or "unknown"
    channel_id = data.get("channel_id")

    if not text:
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Usage: /hire <department> [employee names...]"},
            status_code=200,
        )

    dept, *names = text.split()

    async def run():
        try:
            result = await create_staff_core(dept, names or None, None)
            pretty = json.dumps(result, indent=2)
            await _post_channel(channel_id, f"Hiring request from @{user}:\n```{pretty[:2900]}```")
        except Exception as e:
            await _post_channel(channel_id, f"Hiring failed: {e}")

    # Fast ACK for Slack, do the real work async
    asyncio.create_task(run())
    return JSONResponse(
        {"response_type": "ephemeral", "text": f"Creating {dept} team… I’ll post results here."},
        status_code=200,
    )


# --------------------------------
# SLACK: /memory – remember & recall LTM
# --------------------------------

@app.post("/slack/commands/memory")
async def slack_memory(req: Request):
    """
    /memory remember <text>
    /memory recall <query>
    """
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")

    if text.lower().startswith("remember "):
        note = text[len("remember "):]

        async def run():
            try:
                emb = await embed_text(note)
                imp = await importance_score(note)
                await supabase_insert("long_term_memory", {
                    "content": note,
                    "embedding": emb,
                    "tags": [],
                    "importance": imp,
                    "source": "slack",
                    "department": None,
                    "actor": "CEO",
                    "created_at": now_utc_iso(),
                })
            except Exception:
                pass

        asyncio.create_task(run())
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Noted in long-term memory."},
            status_code=200,
        )

    if text.lower().startswith("recall "):
        query = text[len("recall "):]

        async def run():
            try:
                emb = await embed_text(query)
                matches = await supabase_rpc("match_long_term_memory_ranked", {
                    "query_embedding": emb,
                    "match_count": 5,
                    "dept": None,
                    "min_cosine_similarity": 0.15,
                    "half_life_days": 14.0,
                    "alpha": 0.6,
                    "beta": 0.3,
                }) or []
                pretty = json.dumps(matches, indent=2)
                await _post_channel(channel_id, f"Memory recall:\n```{pretty[:2900]}```")
            except Exception as e:
                await _post_channel(channel_id, f"Recall failed: {e}")

        asyncio.create_task(run())
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Recalling… I’ll post results here."},
            status_code=200,
        )

    return JSONResponse(
        {
            "response_type": "ephemeral",
            "text": "Usage: /memory remember <text> | /memory recall <query>",
        },
        status_code=200,
    )


# --------------------------------
# SLACK: /create – Content Factory
# --------------------------------

@app.post("/slack/commands/create")
async def slack_create(req: Request):
    """
    /create ad <brand> <goal>
    /create social <brand>
    /create blog <topic>
    /create email <subject> <topic>
    """
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")
    parts = _parts(text)

    if len(parts) < 2:
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Usage: /create (ad|social|blog|email) <args>"},
            status_code=200,
        )

    kind = parts[0].lower()

    async def run():
        try:
            if kind == "ad":
                brand = parts[1]
                goal = " ".join(parts[2:]) or "increase conversions"
                prompt = (
                    f"Create 3 high-performing ad concepts for {brand} to {goal}. "
                    f"Include platform suggestions, headlines, and primary text."
                )
            elif kind == "social":
                brand = parts[1]
                prompt = (
                    f"Create 7 short social media posts for {brand} for this week. "
                    f"Include hooks, CTAs, and suggested platforms."
                )
            elif kind == "blog":
                topic = " ".join(parts[1:])
                prompt = (
                    f"Create a detailed blog outline and a strong intro section on: {topic}. "
                    f"Make it SEO-friendly."
                )
            elif kind == "email":
                subject = parts[1]
                body_topic = " ".join(parts[2:]) or "warm outreach to potential client"
                prompt = (
                    f"Write a persuasive email with subject '{subject}' about {body_topic}. "
                    f"Include a clear CTA to book a call or reply."
                )
            else:
                await _post_channel(channel_id, "Unknown type. Use ad | social | blog | email.")
                return

            decision = await call_brain(f"[CONTENT_FACTORY] {prompt}")
            await _post_channel(channel_id, f"*Content ({kind})*\n{decision[:3900]}")
        except Exception as e:
            await _post_channel(channel_id, f"Content creation failed: {e}")

    asyncio.create_task(run())
    return JSONResponse(
        {"response_type": "ephemeral", "text": f"Creating {kind} content… I’ll post results here."},
        status_code=200,
    )


# --------------------------------
# SLACK: /leads – simple lead generation
# --------------------------------

@app.post("/slack/commands/leads")
async def slack_leads(req: Request):
    """
    /leads generate niche=<niche> city=<city>

    Example:
      /leads generate niche=real-estate city=las-vegas
    """
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")

    if not text:
        return JSONResponse(
            {"response_type": "ephemeral", "text": "Usage: /leads generate niche=<niche> city=<city>"},
            status_code=200,
        )

    async def run():
        try:
            tokens = text.split()
            if tokens[0].lower() != "generate":
                await _post_channel(channel_id, "Only 'generate' is implemented right now.")
                return

            params: Dict[str, str] = {}
            for token in tokens[1:]:
                if "=" in token:
                    k, v = token.split("=", 1)
                    params[k.lower()] = v

            niche = params.get("niche", "local business")
            city = params.get("city", "your area")

            prompt = (
                f"Generate a list of 10 ideal {niche} leads in {city}. "
                f"For each lead, provide: business_name, guessed contact_name, guessed email pattern, "
                f"and a one-line 'offer angle' we can pitch."
            )

            decision = await call_brain(f"[LEAD_GENERATION] {prompt}")
            await _post_channel(channel_id, f"*Leads for {niche} in {city}*\n{decision[:3900]}")
        except Exception as e:
            await _post_channel(channel_id, f"Lead generation failed: {e}")

    asyncio.create_task(run())
    return JSONResponse(
        {"response_type": "ephemeral", "text": "Lead generation started… I’ll post results here."},
        status_code=200,
    )


# --------------------------------
# TELEGRAM WEBHOOK (optional)
# --------------------------------

@app.post("/telegram/webhook")
async def telegram_webhook(update: Dict[str, Any]):
    """
    Basic Telegram support: Suzie Q responds like in Slack events.
    """
    msg = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    # If no chat id, just OK so Telegram stops retrying
    if not chat_id:
        return {"ok": True}

    memory_snips = ""
    try:
        if text:
            q_emb = await embed_text(text)
            matches = await supabase_rpc("match_long_term_memory", {
                "query_embedding": q_emb,
                "match_count": 6,
                "min_cosine_similarity": 0.20,
                "dept": None,
            }) or []
            memory_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        memory_snips = ""

    try:
        prefix = "You are Suzie Q (CEO). Use relevant memory when helpful.\n"
        if memory_snips:
            prefix += f"Relevant memory:\n{memory_snips}\n\n"
        prompt = prefix + f"User: {text or 'User says nothing. Greet them briefly.'}"
        decision = await call_brain(prompt)
    except Exception:
        decision = "Hi! I’m Suzie Q. I’m online via Telegram. How can I help right now?"

    try:
        await telegram_send_message(chat_id, decision or "Okay!")
    except Exception:
        pass

    try:
        await supabase_insert("memory", {
            "context": text,
            "decision": decision,
            "source": "telegram",
            "timestamp": now_utc_iso(),
        })
    except Exception:
        pass

    return {"ok": True}


# --------------------------------
# AGENTS – department specialists
# --------------------------------

@app.post("/agents/{dept}/{role}/{name}")
async def agent_invoke(dept: str, role: str, name: str, payload: AgentInvokePayload):
    """
    Department-specialized agent endpoint.
    Example agent_webhook:
      https://.../agents/Marketing/Director/Director%20Marketing
    """
    text = (payload.text or payload.context) or ""

    # Department-filtered recall
    mem_snips = ""
    try:
        q_emb = await embed_text(text)
        matches = await supabase_rpc("match_long_term_memory_ranked", {
            "query_embedding": q_emb,
            "match_count": 6,
            "dept": dept,
            "min_cosine_similarity": 0.20,
            "half_life_days": 14.0,
            "alpha": 0.6,
            "beta": 0.3,
        }) or []
        mem_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        mem_snips = ""

    prompt = (
        f"You are an AI {role} for the {dept} department named {name}. "
        f"Be specialized, practical, and concise.\n"
    )
    if mem_snips:
        prompt += f"Relevant department memory:\n{mem_snips}\n\n"
    prompt += f"User: {text}"

    decision = await call_brain(prompt)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": f"{dept}:{role}:{name}",
        "timestamp": now_utc_iso(),
        "department": dept,
        "actor": name,
    })
    return {"agent": name, "role": role, "dept": dept, "decision": decision}


# --------------------------------
# STAFF API
# --------------------------------

@app.post("/staff/create")
async def staff_create(payload: StaffCreatePayload):
    """
    API version of /hire (useful for internal automations).
    """
    result = await create_staff_core(payload.department, payload.employee_names, payload.slack_channel_id)
    return result


@app.get("/staff/list")
async def staff_list(department: Optional[str] = None):
    """
    List staff (optionally filtered by department name).
    """
    if department:
        dep = await sb_get_one("departments", f"select=*&name=eq.{_enc(department)}")
        if not dep:
            return {"ok": True, "staff": []}
        dep_id = dep["id"]
        rows = await supabase_select("staff", f"select=*&department_id=eq.{dep_id}&order=created_at.asc")
    else:
        rows = await supabase_select("staff", "select=*&order=created_at.asc")

    return {"ok": True, "staff": rows or []}


@app.post("/staff/delete")
async def staff_delete(payload: StaffDeletePayload):
    """
    Soft-delete (deactivate) a staff member by id.
    """
    if not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        r = await client.patch(
            f"{SUPABASE_URL}/rest/v1/staff?id=eq.{payload.staff_id}",
            json={"status": "inactive"},
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"Supabase update failed: {r.text}")

    return {"ok": True}


# --------------------------------
# DAILY CEO REPORT (cron)
# --------------------------------

@app.post("/cron/daily-report")
async def daily_report():
    """
    Summarize recent memory entries into an executive report.
    Ideal to trigger from a Render cron job once per day.
    """
    records = await supabase_select("memory", "select=*&order=timestamp.desc&limit=200") or []
    context = (
        "Summarize the last 24 hours of Suzie Q operations into an executive report "
        "with KPIs and next actions.\n"
    )
    for r in records:
        c = r.get("context", "") or ""
        d = r.get("decision", "") or ""
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


# --------------------------------
# MEMORY API (for programmatic use)
# --------------------------------

@app.post("/memory/remember")
async def remember(payload: RememberPayload):
    emb = await embed_text(payload.content)
    imp = (
        payload.importance
        if payload.importance and 1 <= payload.importance <= 5
        else await importance_score(payload.content)
    )
    row = {
        "content": payload.content,
        "embedding": emb,
        "tags": payload.tags or [],
        "importance": imp,
        "source": payload.source or "api",
        "department": payload.department,
        "actor": payload.actor,
        "created_at": now_utc_iso(),
    }
    await supabase_insert("long_term_memory", row)
    return {"ok": True, "importance": imp}


@app.post("/memory/recall")
async def recall(payload: RecallPayload):
    emb = await embed_text(payload.query)
    matches = await supabase_rpc("match_long_term_memory_ranked", {
        "query_embedding": emb,
        "match_count": payload.top_k,
        "min_cosine_similarity": payload.min_similarity,
        "dept": payload.department,
        "half_life_days": 14.0,
        "alpha": 0.6,
        "beta": 0.3,
    })
    return {"ok": True, "matches": matches}


# --------------------------------
# CORE: create staff for a department
# --------------------------------

async def create_staff_core(
    dept_name: str,
    employee_names: Optional[List[str]],
    slack_channel_id: Optional[str],
) -> Dict[str, Any]:
    """
    Shared logic for /hire and /staff/create.
    - Ensures department row
    - Ensures a Director
    - Creates employees
    - Creates reporting_lines manager -> reports
    """
    dept_name = (dept_name or "").strip()
    if not dept_name:
        return {"ok": False, "error": "department is required"}

    # Department get/create (upsert style)
    dep_row = await sb_get_one("departments", f"select=*&name=eq.{_enc(dept_name)}")
    if not dep_row:
        dep_row = await sb_insert_returning("departments", {
            "name": dept_name,
            "slack_channel_id": slack_channel_id or None,
        })
        if not dep_row:
            return {"ok": False, "error": "Failed to create department (check Supabase)."}

    department_id = dep_row["id"]

    # Director get/create
    director_name = f"Director {dept_name.title()}"
    dir_row = await sb_get_one(
        "staff",
        f"select=*&name=eq.{_enc(director_name)}&role=eq.Director&department_id=eq.{department_id}",
    )
    if not dir_row:
        dir_row = await sb_insert_returning("staff", {
            "name": director_name,
            "role": "Director",
            "department_id": department_id,
            "status": "active",
            "agent_webhook": agent_endpoint(dept_name, "Director", director_name),
        })
        if not dir_row:
            return {"ok": False, "error": "Failed to create director (check Supabase)."}

    # Employees
    if employee_names and len(employee_names) > 0:
        base_names = employee_names
    else:
        base_names = [f"{dept_name.title()} Employee {i}" for i in range(1, 6)]

    employee_rows: List[Dict[str, Any]] = []
    for nm in base_names:
        er = await sb_get_one(
            "staff",
            f"select=*&name=eq.{_enc(nm)}&role=eq.Employee&department_id=eq.{department_id}",
        )
        if not er:
            er = await sb_insert_returning("staff", {
                "name": nm,
                "role": "Employee",
                "department_id": department_id,
                "status": "active",
                "agent_webhook": agent_endpoint(dept_name, "Employee", nm),
            })
            if not er:
                return {"ok": False, "error": f"Failed to create employee: {nm}"}
        employee_rows.append(er)

    # Reporting lines: Director -> each Employee
    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
            for er in employee_rows:
                existing = await sb_get_one(
                    "reporting_lines",
                    f"select=*&manager_id=eq.{dir_row['id']}&report_id=eq.{er['id']}",
                )
                if not existing:
                    r = await c.post(f"{SUPABASE_URL}/rest/v1/reporting_lines", json={
                        "manager_id": dir_row["id"],
                        "report_id": er["id"],
                    })
                    r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"reporting_lines error: {e}"}

    return {
        "ok": True,
        "department": {"id": department_id, "name": dept_name},
        "director": {
            "id": dir_row["id"],
            "name": director_name,
            "agent_url": dir_row.get("agent_webhook"),
        },
        "employees": [
            {"id": er["id"], "name": er["name"], "agent_url": er.get("agent_webhook")}
            for er in employee_rows
        ],
    }
