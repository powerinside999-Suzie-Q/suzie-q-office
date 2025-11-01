# app/main.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Suzie Q – Office")

@app.get("/")
def root():
    return {"message": "Suzie Q Office is running"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/slack/events")
async def slack_events(req: Request):
    body = await req.json()
    # Slack URL verification
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})
    # Minimal echo to prove route works
    return {"ok": True, "received": True}

