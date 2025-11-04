# app/main.py — full, cleaned

import os
import json
import urllib.parse
from typing import Optional, List

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from urllib.parse import parse_qs

from app.schemas import (
    SlackEvent,
    TelegramUpdate,
    AgentInvokePayload,
    RememberPayload,
    RecallPayload,
    StaffCreatePayload,
    StaffDeletePayload,
)

from app.utils import (
    call_brain,
    embed_text,
    supabase_insert,
    supabase_select,
    supabase_rpc,
    slack_post_message,
    telegram_send_message,
    now_utc_iso,
    sb_get_one,
    sb_insert_returning,
    agent_endpoint,
    importance_score,            # <-- needed for /memory/remember
    HEADERS_SB,
    SUPABASE_URL,
)

CEO_CHANNEL = os.getenv("CEO_SLACK_CHANNEL_ID", "")  # e.g., C0XXXXXXX

app = FastAPI(title="Suzie Q – Office")

# ---------------- Root & Health ----------------
@app.get("/")
def root():
    return {"message": "Suzie Q Office is running"}

@app.get("/health")
def health():
    return {"ok": True}

# ---------------- Slack Events (GET + POST) ----------------
@app.get("/slack/events")
def slack_events_get():
    return PlainTextResponse("Slack Events endpoint. Use POST.", status_code=200)

@app.post("/slack/events")
async def slack_events(req: Request):
    body = await req.json()

    # URL verification
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    ev = SlackEvent(**body)
    event = ev.event or {}
    if event.get("bot_id"):
        return {"ok": True}

    text = event.get("text") or ""
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # Ranked memory recall (global)
    memory_snips = ""
    try:
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

    prefix = "You are Suzie Q (CEO). Use relevant memory when helpful.\n"
    if memory_snips:
        prefix += f"Relevant memory:\n{memory_snips}\n\n"
    prompt = prefix + f"User: {text}"

    decision = await call_brain(prompt)

    if channel:
        await slack_post_message(channel, decision, thread_ts=thread_ts)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": "slack",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True}

# ---------------- Slack Slash Commands ----------------
@app.post("/slack/commands/hire")
async def slack_hire(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    user = data.get("user_name") or "unknown"
    channel_id = data.get("channel_id")

    if not text:
        return PlainTextResponse("Usage: /hire <department> [names...]", status_code=200)

    dept, *names = text.split()

    try:
        result = await create_staff_core(dept, names or None, None)
        pretty = json.dumps(result, indent=2)
        if channel_id:
            await slack_post_message(channel_id, f"Hiring request from @{user}:\n```{pretty[:2900]}```")
        return PlainTextResponse(f"Creating {dept} team… I’ll post results here.", status_code=200)
    except Exception as e:
        if channel_id:
            await slack_post_message(channel_id, f"Hiring failed: {e}")
        return PlainTextResponse("Hiring request received, but something went wrong. Check channel for details.", status_code=200)

@app.post("/slack/commands/memory")
async def slack_memory(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()

    if text.lower().startswith("remember "):
        note = text[len("remember "):]
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                await c.post(f"{os.getenv('PUBLIC_BASE_URL')}/memory/remember", json={"content": note})
        except Exception:
            # Fallback to direct call without HTTP if PUBLIC_BASE_URL missing
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
        return PlainTextResponse("Noted in long-term memory.", status_code=200)

    elif text.lower().startswith("recall "):
        query = text[len("recall "):]
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
        return PlainTextResponse(pretty[:2800], status_code=200)

    return PlainTextResponse("Usage: /memory remember <text> | recall <query>", status_code=200)

@app.post("/slack/commands/ask")
async def slack_ask(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    question = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")
    if not question:
        return PlainTextResponse("Usage: /ask <question>", status_code=200)
    decision = await call_brain(f"CEO mode: {question}")
    if channel_id:
        await slack_post_message(channel_id, decision)
    return PlainTextResponse("Sent to Suzie Q...", status_code=200)

# Generic echo if you registered /slack/commands without a subpath
@app.post("/slack/commands")
async def slack_commands(
    command: str = Form(None),
    text: str = Form(None),
    user_id: str = Form(None),
    channel_id: str = Form(None),
    response_url: str = Form(None),
    token: str = Form(None),
    team_id: str = Form(None),
):
    reply = f"Command: {command or ''}\nArgs: {text or ''}"
    return PlainTextResponse(reply, status_code=200)

# ---------------- Telegram Webhook ----------------
@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    msg = update.get("message") or update.get("edited_message") or \
          update.get("channel_post") or update.get("edited_channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    memory_snips = ""
    try:
        if text:
            q_emb = await embed_text(text)
            matches = await supabase_rpc("match_long_term_memory_ranked", {
                "query_embedding": q_emb,
                "match_count": 6,
                "min_cosine_similarity": 0.20,
                "dept": None,
                "half_life_days": 14.0,
                "alpha": 0.6,
                "beta": 0.3,
            }) or []
            memory_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        memory_snips = ""

    try:
        prefix = "You are Suzie Q (CEO). Use relevant memory when helpful.\n"
        if memory_snips:
            prefix += f"Relevant memory:\n{memory_snips}\n\n"
        prompt = prefix + f"User: {text or 'Respond briefly and introduce yourself.'}"
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

# ---------------- Agents (Directors/Employees) ----------------
@app.post("/agents/{dept}/{role}/{name}")
async def agent_invoke(dept: str, role: str, name: str, payload: AgentInvokePayload):
    text = (payload.text or payload.context) or ""

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

# ---------------- Staff APIs ----------------
@app.post("/staff/create", name="staff_create")
async def staff_create(payload: StaffCreatePayload):
    result = await create_staff_core(payload.department, payload.employee_names, payload.slack_channel_id)
    return result

@app.get("/staff/list")
async def staff_list(department: Optional[str] = None):
    if department:
        dep = await sb_get_one("departments", f"select=*&name=eq.{urllib.parse.quote(department)}")
        if not dep:
            return {"ok": True, "staff": []}
        dep_id = dep["id"]
        rows = await supabase_select("staff", f"select=*&department_id=eq.{dep_id}&order=created_at.asc")
    else:
        rows = await supabase_select("staff", "select=*&order=created_at.asc")
    return {"ok": True, "staff": rows or []}

@app.post("/staff/delete")
async def staff_delete(payload: StaffDeletePayload):
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

# ---------------- Simple Department Router (optional) ----------------
@app.post("/route/{dept}")
async def route_to_department(dept: str, request: Request):
    body = await request.json()
    text: str = body.get("text") or ""
    channel: Optional[str] = body.get("channel")
    thread_ts: Optional[str] = body.get("thread_ts")
    target: Optional[str] = body.get("target")

    role = "Director" if (not target or target.lower() == "director") else "Employee"
    name = f"{dept.title()} {target.title()}" if target else f"Director {dept.title()}"

    mem_snips = ""
    try:
        q_emb = await embed_text(text)
        matches = await supabase_rpc("match_long_term_memory_ranked", {
            "query_embedding": q_emb,
            "match_count": 6,
            "min_cosine_similarity": 0.20,
            "dept": dept,
            "half_life_days": 14.0,
            "alpha": 0.6,
            "beta": 0.3,
        }) or []
        mem_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        mem_snips = ""

    prompt = (
        f"You are an AI {role} for the {dept} department named {name}. "
        f"Approve, revise, or produce the best decision. Be concise.\n"
    )
    if mem_snips:
        prompt += f"Relevant department memory:\n{mem_snips}\n\n"
    prompt += f"User: {text}"

    decision = await call_brain(prompt)

    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": f"router:{dept}:{target or 'director'}",
        "timestamp": now_utc_iso(),
        "department": dept,
        "actor": name,
    })

    if channel:
        await slack_post_message(channel, f"[{dept.upper()} {role}] {decision}", thread_ts=thread_ts)

    return {"dept": dept, "target": target or "director", "decision": decision}

# ---------------- Daily CEO Report ----------------
@app.post("/cron/daily-report")
async def daily_report():
    records = await supabase_select("memory", "select=*&order=timestamp.desc&limit=200") or []
    context = "Summarize the last 24 hours of Suzie Q operations into an executive report with KPIs and next actions.\n"
    for r in records:
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

# ---------------- Memory API (vector) ----------------
@app.post("/memory/remember")
async def remember(payload: RememberPayload):
    emb = await embed_text(payload.content)
    imp = payload.importance if (payload.importance and 1 <= payload.importance <= 5) else await importance_score(payload.content)
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

# ---------------- CORE: create staff (shared) ----------------
async def create_staff_core(dept_name: str, employee_names: Optional[List[str]], slack_channel_id: Optional[str]):
    dept_name = (dept_name or "").strip()
    if not dept_name:
        return {"ok": False, "error": "department is required"}

    # Department get/create
    dep_row = await sb_get_one("departments", f"select=*&name=eq.{urllib.parse.quote(dept_name)}")
    if not dep_row:
        dep_row = await sb_insert_returning("departments", {
            "name": dept_name,
            "slack_channel_id": slack_channel_id or None
        })
        if not dep_row:
            return {"ok": False, "error": "Failed to create department (check SUPABASE_* env & pgcrypto extension)."}
    department_id = dep_row["id"]

    # Director get/create
    director_name = f"Director {dept_name.title()}"
    dir_row = await sb_get_one(
        "staff",
        f"select=*&name=eq.{urllib.parse.quote(director_name)}&role=eq.Director&department_id=eq.{department_id}"
    )
    if not dir_row:
        dir_row = await sb_insert_returning("staff", {
            "name": director_name,
            "role": "Director",
            "department_id": department_id,
            "status": "active",
            "agent_webhook": agent_endpoint(dept_name, "Director", director_name)
        })
        if not dir_row:
            return {"ok": False, "error": "Failed to create director (check Supabase)."}

    # Employees
    base_names: List[str]
    if employee_names and len(employee_names) > 0:
        base_names = employee_names
    else:
        base_names = [f"{dept_name.title()} Employee {i}" for i in range(1, 6)]

    employee_rows = []
    for nm in base_names:
        er = await sb_get_one(
            "staff",
            f"select=*&name=eq.{urllib.parse.quote(nm)}&role=eq.Employee&department_id=eq.{department_id}"
        )
        if not er:
            er = await sb_insert_returning("staff", {
                "name": nm,
                "role": "Employee",
                "department_id": department_id,
                "status": "active",
                "agent_webhook": agent_endpoint(dept_name, "Employee", nm)
            })
            if not er:
                return {"ok": False, "error": f"Failed to create employee: {nm}"}
        employee_rows.append(er)

    # Reporting lines
    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
            for er in employee_rows:
                existing = await sb_get_one(
                    "reporting_lines",
                    f"select=*&manager_id=eq.{dir_row['id']}&report_id=eq.{er['id']}"
                )
                if not existing:
                    r = await c.post(f"{SUPABASE_URL}/rest/v1/reporting_lines", json={
                        "manager_id": dir_row["id"],
                        "report_id": er["id"]
                    })
                    if r.status_code >= 400:
                        return {"ok": False, "error": f"reporting_lines insert failed: {r.text}"}
    except Exception as e:
        return {"ok": False, "error": f"reporting_lines error: {e}"}

    return {
        "ok": True,
        "department": {"id": department_id, "name": dept_name},
        "director": {
            "id": dir_row["id"], "name": director_name, "agent_url": dir_row.get("agent_webhook")
        },
        "employees": [
            {"id": er["id"], "name": er["name"], "agent_url": er.get("agent_webhook")}
            for er in employee_rows
        ],
    }

from app.schemas import RnDBootstrapPayload, RnDProjectCreate, RnDExperimentCreate, IngestWebPayload

@app.post("/rnd/bootstrap")
async def rnd_bootstrap(payload: RnDBootstrapPayload):
    dept = payload.department.strip()
    n = max(2, min(12, payload.researchers or 5))  # cap reasonable team size

    # Create/get Director of R&D
    director_name = f"Director R&D {dept.title()}"
    dir_row = await sb_get_one(
        "staff",
        f"select=*&name=eq.{urllib.parse.quote(director_name)}&role=eq.Director&department_id=eq.null"  # let’s create under department row
    )

    # ensure department row
    dep_row = await sb_get_one("departments", f"select=*&name=eq.{urllib.parse.quote(dept)}")
    if not dep_row:
        dep_row = await sb_insert_returning("departments", {"name": dept})
    dep_id = dep_row["id"]

    if not dir_row:
        dir_row = await sb_insert_returning("staff", {
            "name": director_name,
            "role": "Director",
            "department_id": dep_id,
            "status": "active",
            "agent_webhook": agent_endpoint(dept, "Director", director_name)
        })

    # Create N researchers
    researchers = []
    for i in range(1, n + 1):
        name = f"{dept.title()} R&D Researcher {i}"
        r = await sb_get_one(
            "staff",
            f"select=*&name=eq.{urllib.parse.quote(name)}&role=eq.Employee&department_id=eq.{dep_id}"
        )
        if not r:
            r = await sb_insert_returning("staff", {
                "name": name,
                "role": "Employee",
                "department_id": dep_id,
                "status": "active",
                "agent_webhook": agent_endpoint(dept, "Employee", name)
            })
        researchers.append(r)

    # Reporting lines to Director
    async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
        for er in researchers:
            exists = await sb_get_one(
                "reporting_lines",
                f"select=*&manager_id=eq.{dir_row['id']}&report_id=eq.{er['id']}"
            )
            if not exists:
                r = await c.post(f"{SUPABASE_URL}/rest/v1/reporting_lines", json={
                    "manager_id": dir_row["id"],
                    "report_id": er["id"]
                })
                if r.status_code >= 400:
                    return {"ok": False, "error": f"reporting_lines failed: {r.text}"}

    return {
        "ok": True,
        "department": dept,
        "director": {"id": dir_row["id"], "name": dir_row["name"], "agent_url": dir_row.get("agent_webhook")},
        "researchers": [
            {"id": r["id"], "name": r["name"], "agent_url": r.get("agent_webhook")}
            for r in researchers
        ]
    }
