# app/main.py
import os
import httpx
from urllib.parse import parse_qs
from fastapi.responses import PlainTextResponse
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Optional
import os
import httpx, os, urllib.parse, json
from fastapi.responses import PlainTextResponse

from app.schemas import (
    SlackEvent,
    TelegramUpdate,
    AgentInvokePayload,
    RememberPayload,
    RecallPayload,
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
)

CEO_CHANNEL = os.getenv("CEO_SLACK_CHANNEL_ID", "")  # e.g., C0XXXXXXX

app = FastAPI(title="Suzie Q – Office")

# ------------- Root & Health -------------
@app.get("/")
def root():
    return {"message": "Suzie Q Office is running"}

@app.get("/health")
def health():
    return {"ok": True}

# ------------- Slack Events (GET + POST) -------------
@app.get("/slack/events")
def slack_events_get():
    # Convenience: lets you hit the route in a browser without a 405
    return PlainTextResponse("Slack Events endpoint. Use POST.", status_code=200)

@app.post("/slack/events")
async def slack_events(req: Request):
    """
    Handles Slack Event Subscriptions (POST JSON).
    - URL verification returns challenge
    - app_mention / message events: recall memory -> call brain -> reply -> log memory
    """
    body = await req.json()

    # URL verification
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    ev = SlackEvent(**body)
    event = (ev.event or {})
    if event.get("bot_id"):
        # ignore the bot's own posts
        return {"ok": True}

    text = event.get("text") or ""
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # Try to recall relevant memory first (global, not dept-filtered)
    memory_snips = ""
    try:
        q_emb = await embed_text(text)
        matches = await supabase_rpc("match_long_term_memory_ranked", {
    "query_embedding": q_emb,
    "match_count": 6,
    "dept": None,                 # or dept for agents route
    "min_cosine_similarity": 0.20,
    "half_life_days": 14.0,       # tune: smaller = favor fresher memories
    "alpha": 0.6,                 # weight for importance
    "beta": 0.3                   # weight for frequency
}) or []

        memory_snips = "\n".join([f"- {m['content']}" for m in matches])
    except Exception:
        memory_snips = ""

    prefix = "You are Suzie Q (CEO). Use relevant memory when helpful.\n"
    if memory_snips:
        prefix += f"Relevant memory:\n{memory_snips}\n\n"
    prompt = prefix + f"User: {text}"

    decision = await call_brain(prompt)

    # Post back to Slack (threaded when possible)
    if channel:
        await slack_post_message(channel, decision, thread_ts=thread_ts)

    # Log to memory table (short-term activity log)
    await supabase_insert("memory", {
        "context": text,
        "decision": decision,
        "source": "slack",
        "timestamp": now_utc_iso(),
    })
    return {"ok": True}
from fastapi import Form
from urllib.parse import parse_qs
from app.utils import slack_post_message

@app.post("/slack/commands/hire")
async def slack_hire(req: Request):
    # Parse Slack's form-encoded body
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    user = data.get("user_name") or "unknown"
    channel_id = data.get("channel_id")

    if not text:
        return PlainTextResponse("Usage: /hire <department> [names...]", status_code=200)

    # Immediately acknowledge to avoid 'dispatch_failed'
    # (Slack requires a 200 OK within ~3s)
    # We'll do the work after returning.
    try:
        # Fire-and-forget background call
        dept, *names = text.split()
        payload = {"department": dept, "employee_names": names} if names else {"department": dept}
        base = os.getenv("PUBLIC_BASE_URL") or ""
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/staff/create", json=payload)
            msg = r.text[:2800]
        # Post the result back to the channel
        if channel_id:
            await slack_post_message(channel_id, f"Hiring request from @{user}:\n{msg}")
        return PlainTextResponse(f"Creating {dept} team… I’ll post results here.", status_code=200)
    except Exception as e:
        # Post error to channel but still return 200 to Slack
        if channel_id:
            await slack_post_message(channel_id, f"Hiring failed: {e}")
        return PlainTextResponse("Hiring request received, but something went wrong. Check channel for details.", status_code=200)

@app.post("/slack/commands/memory")
async def slack_memory(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = data.get("text", "").strip()
    if text.lower().startswith("remember "):
        note = text[len("remember "):]
        await httpx.AsyncClient().post(f"{os.getenv('PUBLIC_BASE_URL')}/memory/remember",
                                       json={"content": note})
        return PlainTextResponse("Noted in long-term memory.", status_code=200)
    elif text.lower().startswith("recall "):
        query = text[len("recall "):]
        r = await httpx.AsyncClient().post(f"{os.getenv('PUBLIC_BASE_URL')}/memory/recall",
                                           json={"query": query, "top_k": 5})
        return PlainTextResponse(r.text[:2800], status_code=200)
    return PlainTextResponse("Usage: /memory remember <text> | recall <query>", status_code=200)


@app.post("/slack/commands/ask")
async def slack_ask(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    question = data.get("text", "").strip()
    channel_id = data.get("channel_id")
    if not question:
        return PlainTextResponse("Usage: /ask <question>", status_code=200)
    decision = await call_brain(f"CEO mode: {question}")
    await slack_post_message(channel_id, decision)
    return PlainTextResponse("Sent to Suzie Q...", status_code=200)


# ------------- Slack Slash Commands (form-encoded) -------------
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
    """
    Handles Slack Slash Commands (POST form-encoded).
    Set your command's Request URL to /slack/commands.
    """
    # Minimal echo to confirm wiring; expand with routing as needed
    reply = f"Command: {command or ''}\nArgs: {text or ''}"
    return PlainTextResponse(reply, status_code=200)

# ------------- Telegram Webhook -------------
@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    # 1) Extract chat_id & text safely from multiple update types
    msg = update.get("message") or update.get("edited_message") or \
          update.get("channel_post") or update.get("edited_channel_post") or {}
    chat = (msg.get("chat") or {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    # If we can't reply (no chat), just 200 OK so Telegram stops retrying
    if not chat_id:
        return {"ok": True}

    # 2) Try recall + brain, but never crash if they fail
    decision = None

    # Optional recall (safe-wrap)
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

    # Call brain (safe-wrap)
    try:
        prefix = "You are Suzie Q (CEO). Use relevant memory when helpful.\n"
        if memory_snips:
            prefix += f"Relevant memory:\n{memory_snips}\n\n"
        prompt = prefix + f"User: {text or 'Respond briefly and introduce yourself.'}"
        decision = await call_brain(prompt)
    except Exception:
        # Fallback if brain is down
        decision = "Hi! I’m Suzie Q. I’m online via Telegram. How can I help right now?"

    # 3) Send reply (safe-wrap: don’t crash if token missing)
    try:
        await telegram_send_message(chat_id, decision or "Okay!")
    except Exception:
        pass

    # 4) Log memory (safe-wrap)
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

# ------------- Agents (Directors/Employees) -------------
@app.post("/agents/{dept}/{role}/{name}")
async def agent_invoke(dept: str, role: str, name: str, payload: AgentInvokePayload):
    """
    Department-specialized agent endpoint.
    """
    text = (payload.text or payload.context) or ""

    # Department-filtered recall
    mem_snips = ""
    try:
        q_emb = await embed_text(text)
        matches = await supabase_rpc("match_long_term_memory_ranked", {
    "query_embedding": q_emb,
    "match_count": 6,
    "dept": None,                 # or dept for agents route
    "min_cosine_similarity": 0.20,
    "half_life_days": 14.0,       # tune: smaller = favor fresher memories
    "alpha": 0.6,                 # weight for importance
    "beta": 0.3                   # weight for frequency
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
from app.schemas import StaffCreatePayload, StaffDeletePayload
from app.utils import sb_get_one, sb_insert_returning, agent_endpoint, slack_post_message

@app.post("/staff/create")
async def staff_create(payload: StaffCreatePayload):
    """
    Create a department (if missing), a Director, and N employees.
    Wire reporting_lines (employees -> director).
    Returns the created/located records and ready-to-call agent URLs.
    """
    dept_name = payload.department.strip()
    if not dept_name:
        raise HTTPException(status_code=400, detail="department is required")

    # 1) Department: get or create
    # NOTE: spaces must be url-encoded for the filter; use eq.<value>
    dep_row = await sb_get_one("departments", f"select=*&name=eq.{urllib.parse.quote(dept_name)}")
    if not dep_row:
        dep_row = await sb_insert_returning("departments", {
            "name": dept_name,
            "slack_channel_id": payload.slack_channel_id or None
        })
    department_id = dep_row["id"]

    # 2) Director: get or create by (name, role, department_id)
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
            "agent_webhook": agent_endpoint(dept_name, "Director", director_name)  # public agent endpoint
        })

    created = {"department": dep_row, "director": dir_row, "employees": []}

    # 3) Employees
    count = max(1, payload.employees_count or 5)
    if payload.employee_names and len(payload.employee_names) > 0:
        base_names = payload.employee_names[:count]
        # ensure list length = count
        if len(base_names) < count:
            base_names += [f"{dept_name.title()} Employee {i}" for i in range(len(base_names)+1, count+1)]
    else:
        base_names = [f"{dept_name.title()} Employee {i}" for i in range(1, count+1)]

    employee_rows = []
    for nm in base_names:
        emp_row = await sb_get_one(
            "staff",
            f"select=*&name=eq.{urllib.parse.quote(nm)}&role=eq.Employee&department_id=eq.{department_id}"
        )
        if not emp_row:
            emp_row = await sb_insert_returning("staff", {
                "name": nm,
                "role": "Employee",
                "department_id": department_id,
                "status": "active",
                "agent_webhook": agent_endpoint(dept_name, "Employee", nm)
            })
        employee_rows.append(emp_row)
    created["employees"] = employee_rows

    # 4) Reporting lines (employees -> director)
    for er in employee_rows:
        # Check if reporting already exists to avoid duplicates:
        existing = await sb_get_one(
            "reporting_lines",
            f"select=*&manager_id=eq.{dir_row['id']}&report_id=eq.{er['id']}"
        )
        if not existing:
            await sb_insert_returning("reporting_lines", {
                "manager_id": dir_row["id"],
                "report_id": er["id"]
            })

    # 5) Optional: announce in Slack CEO channel
    ceo_channel = os.getenv("CEO_SLACK_CHANNEL_ID", "")
    if ceo_channel:
        try:
            lines = [
                f"Department **{dept_name}** ready.",
                f"Director: {director_name} → {dir_row.get('agent_webhook','')}",
            ] + [f"Employee: {er['name']} → {er.get('agent_webhook','')}" for er in employee_rows]
            await slack_post_message(ceo_channel, "\n".join(lines))
        except Exception:
            pass

    return {
        "ok": True,
        "department": {"id": department_id, "name": dept_name},
        "director": {
            "id": dir_row["id"],
            "name": director_name,
            "agent_url": dir_row.get("agent_webhook")
        },
        "employees": [
            {"id": er["id"], "name": er["name"], "agent_url": er.get("agent_webhook")}
            for er in employee_rows
        ],
    }

@app.get("/staff/list")
async def staff_list(department: Optional[str] = None):
    """
    List staff (optionally by department name).
    """
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
    """
    Soft-delete (deactivate) a staff member by id.
    """
    # Soft delete by status=inactive (safer than hard delete)
    if not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    async with httpx.AsyncClient(timeout=60, headers=HEADERS_SB) as client:
        # Patch-like behavior using 'Prefer: resolution=merge-duplicates' requires UPSERT constraints.
        # Here we use RPC-free update via ?id=eq. filter.
        r = await client.patch(
            f"{SUPABASE_URL}/rest/v1/staff?id=eq.{payload.staff_id}",
            json={"status": "inactive"},
        )
        # Some Supabase setups require headers["Prefer"] = "return=representation"
        # but we don't need the body here.
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"Supabase update failed: {r.text}")
    return {"ok": True}


# ------------- Simple Department Router (optional) -------------
@app.post("/route/{dept}")
async def route_to_department(dept: str, request: Request):
    """
    Minimal router:
    Input JSON: {text, channel?, thread_ts?, target?}
    target: "director" or "employee-3" (freeform label)
    """
    body = await request.json()
    text: str = body.get("text") or ""
    channel: Optional[str] = body.get("channel")
    thread_ts: Optional[str] = body.get("thread_ts")
    target: Optional[str] = body.get("target")

    role = "Director" if (not target or target.lower() == "director") else "Employee"
    name = f"{dept.title()} {target.title()}" if target else f"Director {dept.title()}"

    # Dept recall
    mem_snips = ""
    try:
        q_emb = await embed_text(text)
        matches = await supabase_rpc("match_long_term_memory", {
            "query_embedding": q_emb,
            "match_count": 6,
            "min_cosine_similarity": 0.20,
            "dept": dept,
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

# ------------- Daily CEO Report -------------
@app.post("/cron/daily-report")
async def daily_report():
    """
    Summarize last ~200 entries from memory (you can filter by timestamp on Supabase).
    Post to CEO Slack channel if configured.
    """
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

# ------------- Memory API (vector) -------------
@app.post("/memory/remember")
async def remember(payload: RememberPayload):
    emb = await embed_text(payload.content)
    imp = payload.importance if payload.importance and 1 <= payload.importance <= 5 else await importance_score(payload.content)
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
    matches = await supabase_rpc("match_long_term_memory", {
        "query_embedding": emb,
        "match_count": payload.top_k,
        "min_cosine_similarity": payload.min_similarity,
        "dept": payload.department,
    })
    return {"ok": True, "matches": matches}

