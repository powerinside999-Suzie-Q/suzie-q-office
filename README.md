
# Suzie Q – FastAPI OS

**Endpoints**
- POST /slack/events
- POST https://suzie-q-office.onrender.com/telegram/webhook
- POST /agents/{dept}/{role}/{name}
- POST /cron/daily-report

**Deploy on Render**
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set env vars in Render dashboard.
