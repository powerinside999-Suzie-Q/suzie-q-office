# app/main.py
import os
import json
import asyncio
import urllib.parse
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, Request, Form, HTTPException, Body
from fastapi.responses import JSONResponse, PlainTextResponse
from urllib.parse import parse_qs

from app.schemas import (
    # events & chat
    SlackEvent,
    TelegramUpdate,
    AgentInvokePayload,
    # memory
    RememberPayload,
    RecallPayload,
    # staff
    StaffCreatePayload,
    StaffDeletePayload,
    # R&D
    RnDBootstrapPayload,
    RnDProjectCreate,
    RnDExperimentCreate,
    IngestWebPayload,
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

CEO_CHANNEL = os.getenv("CEO_SLACK_CHANNEL_ID", "")  # optional
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

app = FastAPI(title="Suzie Q – Office")

# ------------------------------ Helpers ------------------------------
def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")

async def _post_channel(channel_id: Optional[str], text: str, thread_ts: Optional[str] = None):
    if channel_id:
        await slack_post_message(channel_id, text, thread_ts=thread_ts)

# ------------------------------ Root & Health ------------------------------
@app.get("/")
def root():
    return {"message": "Suzie Q Office is running"}

@app.get("/health")
def health():
    return {"ok": True}

# ------------------------------ Slack Events ------------------------------
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

    # memory recall (ranked)
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

# ------------------------------ Slack Slash: Hire ------------------------------
@app.post("/slack/commands/hire")
async def slack_hire(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    text = (data.get("text") or "").strip()
    user = data.get("user_name") or "unknown"
    channel_id = data.get("channel_id")

    if not text:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /hire <department> [names...]"},
                            status_code=200)
    dept, *names = text.split()

    async def run():
        try:
            result = await create_staff_core(dept, names or None, None)
            pretty = json.dumps(result, indent=2)
            await _post_channel(channel_id, f"Hiring request from @{user}:\n```{pretty[:2900]}```")
        except Exception as e:
            await _post_channel(channel_id, f"Hiring failed: {e}")

    asyncio.create_task(run())
    return JSONResponse({"response_type": "ephemeral",
                         "text": f"Creating {dept} team… I’ll post results here."},
                        status_code=200)

# ------------------------------ Slack Slash: Memory ------------------------------
@app.post("/slack/commands/memory")
async def slack_memory(req: Request):
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
        return JSONResponse({"response_type": "ephemeral", "text": "Noted in long-term memory."}, status_code=200)

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
        return JSONResponse({"response_type": "ephemeral", "text": "Recalling… posting results."}, status_code=200)

    return JSONResponse({"response_type": "ephemeral",
                         "text": "Usage: /memory remember <text> | recall <query>"},
                        status_code=200)

# ------------------------------ Slack Slash: Ask ------------------------------
@app.post("/slack/commands/ask")
async def slack_ask(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    question = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")
    if not question:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /ask <question>"},
                            status_code=200)

    async def run():
        try:
            decision = await call_brain(f"CEO mode: {question}")
            await _post_channel(channel_id, decision)
        except Exception as e:
            await _post_channel(channel_id, f"/ask failed: {e}")

    asyncio.create_task(run())
    return JSONResponse({"response_type": "ephemeral", "text": "Sent to Suzie Q…"}, status_code=200)

# ------------------------------ Slack Slash: R&D / Ingest / Report ------------------------------
# ---------- R&D LIST HELPERS ----------
async def list_projects(dept: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    if dept:
        qs = f"select=*&department=eq.{_enc(dept)}&order=created_at.desc&limit={limit}"
    else:
        qs = f"select=*&order=created_at.desc&limit={limit}"
    return await supabase_select("rnd_projects", qs) or []

async def list_experiments(project_id: Optional[str] = None, dept: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    # Fast paths
    if project_id:
        qs = f"select=*&project_id=eq.{_enc(project_id)}&order=created_at.desc&limit={limit}"
        return await supabase_select("rnd_experiments", qs) or []

    if not dept:
        # No dept filter → just latest experiments
        qs = f"select=*&order=created_at.desc&limit={limit}"
        return await supabase_select("rnd_experiments", qs) or []

    # Dept filter → fetch project ids for that department, then filter experiments by IN (...)
    projects = await list_projects(dept=dept, limit=200)
    if not projects:
        return []
    ids = [p["id"] for p in projects if p.get("id")]
    # Build in.(...) clause; URL-safe
    idlist = ",".join(ids)
    qs = f"select=*&project_id=in.({idlist})&order=created_at.desc&limit={limit}"
    return await supabase_select("rnd_experiments", qs) or []

def fmt_projects(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No projects found."
    lines = []
    for r in rows[:20]:
        lines.append(f"- *{r.get('title','Untitled')}*  _(dept: {r.get('department','?')})_\n  id: `{r.get('id','')}`  status: {r.get('status','')}")
    return "\n".join(lines)

def fmt_experiments(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No experiments found."
    lines = []
    for r in rows[:20]:
        lines.append(f"- *{r.get('hypothesis','(no hypothesis)')}*\n  id: `{r.get('id','')}`  project_id: `{r.get('project_id','')}`  status: {r.get('status','')}")
    return "\n".join(lines)

@app.post("/slack/commands/rnd")
async def slack_rnd(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    channel_id = data.get("channel_id")
    args = (data.get("text") or "").strip()

    if not args:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /rnd <dept> bootstrap [N] | project \"Title\" \"Goal\" | experiment <projectId> \"Hypothesis\""},
                            status_code=200)

    parts = args.split()
    dept = parts[0]
    sub = parts[1].lower() if len(parts) > 1 else ""

    async def run():
        try:
            if sub == "list":
                # /rnd list
                # /rnd list <dept>
                # /rnd list <dept> projects
                # /rnd list <dept> experiments
                # /rnd list projects
                # /rnd list experiments
                tail = args.split("list", 1)[1].strip() if "list" in args else ""
                parts_tail = tail.split()
                kind = None
                dept_filter = None
                project_id = None

                # quick parse
                if len(parts_tail) == 0:
                    # default: list latest projects
                    kind = "projects"
                elif len(parts_tail) == 1:
                    if parts_tail[0].lower() in ["projects", "experiments"]:
                        kind = parts_tail[0].lower()
                    else:
                        dept_filter = parts_tail[0]
                        kind = "projects"
                else:
                    # e.g., "<dept> projects" or "<dept> experiments" or "project <ID>"
                    if parts_tail[0].lower() in ["project", "proj"]:
                        # /rnd list project <PROJECT_ID>
                        if len(parts_tail) >= 2:
                            project_id = parts_tail[1]
                            kind = "experiments"
                        else:
                            await _post_channel(channel_id, "Usage: /rnd list project <PROJECT_ID>")
                            return
                    else:
                        dept_filter = parts_tail[0]
                        kind = parts_tail[1].lower() if len(parts_tail) > 1 else "projects"

                if kind == "projects":
                    rows = await list_projects(dept=dept_filter)
                    msg = fmt_projects(rows)
                    await _post_channel(channel_id, f"*R&D Projects*{f' (dept: {dept_filter})' if dept_filter else ''}:\n{msg}")
                elif kind == "experiments":
                    rows = await list_experiments(project_id=project_id, dept=dept_filter)
                    msg = fmt_experiments(rows)
                    context = ""
                    if project_id:
                        context = f" (project: `{project_id}`)"
                    elif dept_filter:
                        context = f" (dept: {dept_filter})"
                    await _post_channel(channel_id, f"*R&D Experiments*{context}:\n{msg}")
                else:
                    await _post_channel(channel_id, "Try: `/rnd list`, `/rnd list <dept>`, `/rnd list projects`, `/rnd list <dept> experiments`, or `/rnd list project <PROJECT_ID>`")
                return

            # ------- existing subcommands you already have -------
            if sub == "bootstrap":
                n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
                res = await rnd_bootstrap(RnDBootstrapPayload(department=dept, researchers=n))
                await _post_channel(channel_id, f"R&D bootstrap for *{dept}*:\n```{json.dumps(res, indent=2)[:2900]}```")
            elif sub == "project":
                tail = args.split("project", 1)[1].strip()
                try:
                    title = tail.split('"')[1]
                    goal = tail.split('"')[3] if len(tail.split('"')) > 3 else None
                except Exception:
                    await _post_channel(channel_id, "Usage: /rnd <dept> project \"Title\" \"Goal(optional)\"")
                    return
                r = await rnd_project_create(RnDProjectCreate(department=dept, title=title, goal=goal))
                await _post_channel(channel_id, f"Project created:\n```{json.dumps(r, indent=2)[:2900]}```")
            elif sub == "experiment":
                if len(parts) < 3:
                    await _post_channel(channel_id, "Usage: /rnd <dept> experiment <projectId> \"Hypothesis\"")
                    return
                project_id = parts[2]
                rest = args.split(project_id, 1)[1].strip()
                try:
                    hypothesis = rest.split('"')[1]
                except Exception:
                    await _post_channel(channel_id, "Provide hypothesis in quotes.")
                    return
                r = await rnd_experiment_create(RnDExperimentCreate(project_id=project_id, hypothesis=hypothesis))
                await _post_channel(channel_id, f"Experiment created:\n```{json.dumps(r, indent=2)[:2900]}```")
            else:
                await _post_channel(channel_id, "Subcommand not recognized. Try: list | bootstrap | project | experiment")
        except Exception as e:
            await _post_channel(channel_id, f"R&D command failed: {e}")


    asyncio.create_task(run())
    return JSONResponse({"response_type": "ephemeral",
                         "text": f"R&D command accepted for {dept}. I’ll post results here."},
                        status_code=200)

@app.post("/slack/commands/ingest")
async def slack_ingest(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    args = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")

    if not args:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /ingest <dept> web <url>"},
                            status_code=200)

    parts = args.split()
    if len(parts) < 3 or parts[1].lower() != "web":
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /ingest <dept> web <url>"},
                            status_code=200)
    dept, _, url = parts[0], parts[1], parts[2]

    async def run():
        try:
            r = await ingest_web(IngestWebPayload(department=dept, url=url))
            await _post_channel(channel_id, f"Ingested into {dept} R&D knowledge:\n{url}")
        except Exception as e:
            await _post_channel(channel_id, f"Ingest failed: {e}")

    asyncio.create_task(run())
    return JSONResponse({"response_type": "ephemeral", "text": "On it. Fetching and storing."}, status_code=200)

@app.post("/slack/commands/report")
async def slack_report(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    args = (data.get("text") or "").strip()
    channel_id = data.get("channel_id")

    if not args:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: /report <dept> weekly"},
                            status_code=200)

    parts = args.split()
    dept = parts[0]
    interval = parts[1].lower() if len(parts) > 1 else "weekly"

    async def run():
        try:
            recents = await supabase_select(
                "memory",
                f"select=*&department=eq.{_enc(dept)}&order=timestamp.desc&limit=200"
            ) or []
            context = f"Create a {interval} R&D report for the {dept} department. Summarize projects, experiments, findings, and next actions.\n"
            for r in recents:
                c = r.get("context", "")
                d = r.get("decision", "")
                context += f"- Context: {c}\n  Decision: {d}\n"
            summary = await call_brain(context)
            await _post_channel(channel_id, f"[{dept} R&D {interval.title()} Report]\n{summary}")
            await supabase_insert("memory", {
                "context": f"[{dept} R&D] {interval} report",
                "decision": summary,
                "source": "report",
                "department": dept,
                "actor": "R&D Director",
                "timestamp": now_utc_iso(),
            })
        except Exception as e:
            await _post_channel(channel_id, f"Report failed: {e}")

    asyncio.create_task(run())
    return JSONResponse({"response_type": "ephemeral",
                         "text": f"Generating {interval} report for {dept}."},
                        status_code=200)

# ------------------------------ Slack Slash: Autonomy Controls ------------------------------
@app.post("/slack/commands/mode")
async def slack_mode(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    args = (data.get("text") or "").strip().lower()
    if args not in ["off","approvals","autopilot"]:
        return JSONResponse({"response_type":"ephemeral","text":"Usage: /mode off|approvals|autopilot"}, status_code=200)
    asyncio.create_task(set_mode_internal(args))
    return JSONResponse({"response_type":"ephemeral","text":f"Autonomy mode → {args}"}, status_code=200)

@app.post("/slack/commands/goal")
async def slack_goal(req: Request):
    # /goal "Title" "Description" priority(1-5)
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    channel_id = data.get("channel_id")
    args = (data.get("text") or "").strip()

    def _quoted(i):
        try:
            return args.split('"')[i]
        except Exception:
            return None

    title = _quoted(1)
    desc = _quoted(3)
    pr = 3
    tail = args.split('"')[-1].strip().split()
    if tail and tail[0].isdigit():
        pr = int(tail[0])

    if not title:
        return JSONResponse({"response_type":"ephemeral","text":"Usage: /goal \"Title\" \"Description\" priority(1-5)"}, status_code=200)

    async def run():
        await supabase_insert("goals", {"title": title, "description": desc, "priority": max(1,min(5,pr))})
        await _post_channel(channel_id, f"Goal created: *{title}* (p{pr})")

    asyncio.create_task(run())
    return JSONResponse({"response_type":"ephemeral","text":"Creating goal…"}, status_code=200)

@app.post("/slack/commands/task")
async def slack_task(req: Request):
    # /task <Dept> "Title" [ingest:web url=...]
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    channel_id = data.get("channel_id")
    args = (data.get("text") or "").strip()
    parts = args.split()

    if len(parts) < 1:
        return JSONResponse({"response_type":"ephemeral","text":"Usage: /task <Dept> \"Title\" [ingest:web url=...]"}, status_code=200)

    dept = parts[0]
    try:
        title = args.split('"')[1]
    except Exception:
        return JSONResponse({"response_type":"ephemeral","text":"Provide task title in quotes."}, status_code=200)

    tool = "agent"
    payload = {}
    if "ingest:web" in args:
        tool = "ingest:web"
        if "url=" in args:
            payload["url"] = args.split("url=",1)[1].split()[0]

    async def run():
        await supabase_insert("tasks", {
            "title": title,
            "department": dept,
            "tool": tool,
            "payload": payload,
            "status": "queued",
        })
        await _post_channel(channel_id, f"Task queued for *{dept}*: {title}")

    asyncio.create_task(run())
    return JSONResponse({"response_type":"ephemeral","text":"Queuing task…"}, status_code=200)

@app.post("/slack/commands/tick")
async def slack_tick(req: Request):
    body = await req.body()
    data = {k: v[0] for k, v in parse_qs(body.decode()).items()}
    channel_id = data.get("channel_id")

    async def run():
        res = await autopilot_tick()
        await _post_channel(channel_id, f"Tick done. Planned: {len(res.get('planned',[]))}, Executed: {len(res.get('executed',[]))}")

    asyncio.create_task(run())
    return JSONResponse({"response_type":"ephemeral","text":"Running one autonomy tick…"}, status_code=200)

# ------------------------------ Telegram Webhook ------------------------------
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

# ------------------------------ Agents (Directors/Employees) ------------------------------
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

# ------------------------------ Staff APIs ------------------------------
@app.post("/staff/create", name="staff_create")
async def staff_create(payload: StaffCreatePayload):
    return await create_staff_core(payload.department, payload.employee_names, payload.slack_channel_id)

@app.get("/staff/list")
async def staff_list(department: Optional[str] = None):
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

# ------------------------------ Router (optional) ------------------------------
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

# ------------------------------ Daily CEO Report ------------------------------
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

# ------------------------------ Memory API ------------------------------
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

# ------------------------------ Staff Core ------------------------------
async def create_staff_core(dept_name: str, employee_names: Optional[List[str]], slack_channel_id: Optional[str]):
    dept_name = (dept_name or "").strip()
    if not dept_name:
        return {"ok": False, "error": "department is required"}

    # Department get/create
    dep_row = await sb_get_one("departments", f"select=*&name=eq.{_enc(dept_name)}")
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
        f"select=*&name=eq.{_enc(director_name)}&role=eq.Director&department_id=eq.{department_id}"
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
            f"select=*&name=eq.{_enc(nm)}&role=eq.Employee&department_id=eq.{department_id}"
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

# ------------------------------ R&D Internals ------------------------------
async def rnd_bootstrap(payload: RnDBootstrapPayload) -> Dict[str, Any]:
    dept = payload.department.strip()
    n = max(2, min(12, payload.researchers or 5))

    dep_row = await sb_get_one("departments", f"select=*&name=eq.{_enc(dept)}")
    if not dep_row:
        dep_row = await sb_insert_returning("departments", {"name": dept})
    dep_id = dep_row["id"]

    director_name = f"Director R&D {dept.title()}"
    dir_row = await sb_get_one(
        "staff",
        f"select=*&name=eq.{_enc(director_name)}&role=eq.Director&department_id=eq.{dep_id}"
    )
    if not dir_row:
        dir_row = await sb_insert_returning("staff", {
            "name": director_name,
            "role": "Director",
            "department_id": dep_id,
            "status": "active",
            "agent_webhook": agent_endpoint(dept, "Director", director_name),
        })

    researchers = []
    for i in range(1, n + 1):
        name = f"{dept.title()} R&D Researcher {i}"
        r = await sb_get_one(
            "staff",
            f"select=*&name=eq.{_enc(name)}&role=eq.Employee&department_id=eq.{dep_id}"
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
                    raise HTTPException(status_code=500, detail=f"reporting_lines failed: {r.text}")

    return {
        "ok": True,
        "department": dept,
        "director": {"id": dir_row["id"], "name": dir_row["name"], "agent_url": dir_row.get("agent_webhook")},
        "researchers": [{"id": r["id"], "name": r["name"], "agent_url": r.get("agent_webhook")} for r in researchers]
    }

async def rnd_project_create(payload: RnDProjectCreate) -> Dict[str, Any]:
    """
    Create an R&D project and return the created row.
    If insert returns no body, fetch by (department, title).
    """
    row = await sb_insert_returning("rnd_projects", {
        "department": payload.department,
        "title": payload.title,
        "goal": payload.goal
    })
    if not row:
        # Fallback: fetch the most recent match by dept+title
        q = f"select=*&department=eq.{_enc(payload.department)}&title=eq.{_enc(payload.title)}&order=created_at.desc&limit=1"
        rows = await supabase_select("rnd_projects", q)
        if rows:
            row = rows[0]
        else:
            return {"ok": False, "error": "Insert succeeded but no row returned; and lookup found nothing. Check RLS/policies."}
    return {"ok": True, "project": row}

async def rnd_experiment_create(payload: RnDExperimentCreate) -> Dict[str, Any]:
    """
    Create an R&D experiment and return the created row.
    If insert returns no body, fetch by (project_id, hypothesis).
    """
    row = await sb_insert_returning("rnd_experiments", {
        "project_id": payload.project_id,
        "hypothesis": payload.hypothesis,
        "method": payload.method,
        "metrics": payload.metrics or []
    })
    if not row:
        q = (
            "select=*&"
            f"project_id=eq.{_enc(payload.project_id)}&"
            f"hypothesis=eq.{_enc(payload.hypothesis)}&"
            "order=created_at.desc&limit=1"
        )
        rows = await supabase_select("rnd_experiments", q)
        if rows:
            row = rows[0]
        else:
            return {"ok": False, "error": "Insert succeeded but no row returned; and lookup found nothing. Check RLS/policies."}
    return {"ok": True, "experiment": row}

    async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
        kr = await c.post(f"{SUPABASE_URL}/rest/v1/rnd_knowledge", json={
            "department": payload.department,
            "source_url": payload.url,
            "title": title,
            "content": content,
            "tags": ["web_ingest"]
        })
        if kr.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"Knowledge insert failed: {kr.text}")

    emb = await embed_text(f"{title}\n{content[:2000]}")
    await supabase_insert("long_term_memory", {
        "content": f"[{payload.department} R&D] {title}\n{payload.url}",
        "embedding": emb,
        "tags": ["rnd", payload.department, "web"],
        "importance": 3,
        "source": "ingest:web",
        "department": payload.department,
        "actor": "R&D Ingestion",
        "created_at": now_utc_iso(),
    })

    return {"ok": True, "title": title}

# ------------------------------ Autonomy: policy/goals/tasks/tick ------------------------------
@app.get("/autonomy/policy")
async def get_policy():
    rows = await supabase_select("autonomy_policy", "select=*")
    return rows[0] if rows else {"mode":"approvals","risk_tolerance":2,"auto_delegate":True,"max_parallel_tasks":3}

@app.post("/autonomy/policy")
async def set_policy(policy: dict = Body(...)):
    rows = await supabase_select("autonomy_policy","select=*")
    if rows:
        existing_id = rows[0]["id"]
        async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
            r = await c.patch(f"{SUPABASE_URL}/rest/v1/autonomy_policy?id=eq.{existing_id}", json=policy)
            r.raise_for_status()
    else:
        await supabase_insert("autonomy_policy", policy)
    return {"ok": True}

async def set_mode_internal(mode: str):
    assert mode in ["off","approvals","autopilot"]
    await set_policy({"mode": mode})

@app.post("/autonomy/mode/{mode}")
async def set_mode(mode: str):
    await set_mode_internal(mode)
    return {"ok": True}

@app.post("/goals")
async def create_goal(goal: dict = Body(...)):
    await supabase_insert("goals", goal)
    return {"ok": True}

@app.get("/goals")
async def list_goals():
    return await supabase_select("goals", "select=*&order=created_at.desc")

@app.post("/tasks")
async def create_task(task: dict = Body(...)):
    await supabase_insert("tasks", task)
    return {"ok": True}

@app.get("/tasks")
async def list_tasks(status: Optional[str] = None):
    qs = "select=*&order=created_at.desc"
    if status:
        qs = f"select=*&status=eq.{_enc(status)}&order=created_at.desc"
    return await supabase_select("tasks", qs)

@app.post("/autopilot/tick")
async def autopilot_tick():
    pol = await get_policy()
    if pol.get("mode") == "off":
        return {"ok": True, "note": "Autonomy OFF"}

    goals = await supabase_select("goals","select=*&status=eq.active&order=priority.asc")
    recent_kpis = await supabase_select("kpis","select=*&order=captured_at.desc&limit=50")
    recent_memory = await supabase_select("memory","select=*&order=timestamp.desc&limit=50")

    context = "You are Suzie Q, CEO. Propose a 1-cycle plan of 3-7 tasks to move active goals forward.\n"
    context += f"Autonomy: {pol.get('mode')} | risk={pol.get('risk_tolerance')} | auto_delegate={pol.get('auto_delegate')}\n"
    context += "Active goals:\n" + "\n".join([f"- ({g.get('priority')}) {g.get('title')}: {g.get('description','')}" for g in goals or []]) + "\n"
    context += "Latest KPIs:\n" + "\n".join([f"- {k.get('name')}: {k.get('value')}{k.get('unit') or ''}" for k in recent_kpis or []]) + "\n"
    plan = await call_brain(context + "Return JSON with tasks=[{title,details,department,tool,payload,importance(1..5)}].")

    try:
        data = json.loads(plan) if isinstance(plan, str) else plan
        tasks = data.get("tasks", [])
    except Exception:
        tasks = []

    created = []
    for t in tasks[: pol.get("max_parallel_tasks", 3)]:
        trow = {
            "title": t.get("title","Untitled"),
            "details": t.get("details",""),
            "department": t.get("department"),
            "assignee": f"Director { (t.get('department') or 'Operations').title() }",
            "tool": (t.get("tool") or "agent").lower(),
            "payload": t.get("payload") or {},
            "importance": max(1, min(5, int(t.get("importance",3)))),
            "status": "queued",
        }
        await supabase_insert("tasks", trow)
        created.append(trow)

    executed = []
    if pol.get("mode") in ["approvals","autopilot"]:
        queue = await supabase_select("tasks","select=*&status=eq.queued&order=created_at.asc&limit=5") or []
        for q in queue:
            tool = (q.get("tool") or "agent").lower()
            dept = q.get("department") or "Operations"
            payload = q.get("payload") or {}
            out = None
            try:
                if tool == "ingest:web":
                    out = await ingest_web(IngestWebPayload(department=dept, url=payload.get("url","")))
                else:
                    role = "Director"
                    name = f"Director {dept.title()}"
                    out = await agent_invoke(dept, role, name, AgentInvokePayload(text=q.get("details","")))
                async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
                    await c.patch(f"{SUPABASE_URL}/rest/v1/tasks?id=eq.{q['id']}", json={"status":"done"})
                executed.append({"id": q["id"], "result": out})
            except Exception as e:
                async with httpx.AsyncClient(timeout=30, headers=HEADERS_SB) as c:
                    await c.patch(f"{SUPABASE_URL}/rest/v1/tasks?id=eq.{q['id']}", json={"status":"failed"})
                executed.append({"id": q["id"], "error": str(e)})

    summary = await call_brain(
        "Summarize the cycle: planned tasks and executed results. Provide 3 next actions." +
        f"\nPlanned: {json.dumps(created)[:2000]}\nExecuted: {json.dumps(executed)[:2000]}"
    )
    await supabase_insert("memory", {
        "context": "[autopilot] tick",
        "decision": summary,
        "source": "autopilot",
        "timestamp": now_utc_iso(),
    })
    if CEO_CHANNEL:
        await slack_post_message(CEO_CHANNEL, f"Autopilot tick summary:\n{summary}")

    return {"ok": True, "planned": created, "executed": executed}
