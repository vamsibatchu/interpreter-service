"""
Interpreter Bridge Service — RTT Edition
-----------------------------------------
Caller dials in → agent asks language → agent asks who to connect → RTT translates both ways
Uses: ElevenLabs (Conversational AI Agent + RTT API) · Twilio (telephony)
"""

import asyncio
import json
import logging
import os
import re

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.rtt_bridge import RTTBridge
from app.session import SessionStore

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
log.info("RTT Bridge main.py loaded")

app = FastAPI(title="Interpreter Bridge — RTT Edition")

# Singletons
session_store = SessionStore()
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")


def clean(val: str) -> str:
    """Strip quotes and whitespace Railway adds around env var values."""
    return val.strip().strip('"').strip("'").strip()


def get_twilio_client():
    """Create a fresh Twilio client reading env vars at call time."""
    sid   = clean(os.getenv("TWILIO_ACCOUNT_SID", "AC72526c0568b936116dd72ece8f2f0718"))
    token = clean(os.getenv("TWILIO_AUTH_TOKEN",  "379b939667adec3d318fd5f35ea82709"))
    log.info(f"Twilio SID: '{sid[:8] if sid else 'EMPTY'}' Token: '{'SET' if token else 'EMPTY'}'")
    return Client(sid, token)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    sid = clean(os.getenv("TWILIO_ACCOUNT_SID", ""))
    return {
        "status":     "Interpreter RTT Bridge running",
        "twilio_sid": sid[:8] + "..." if sid else "NOT SET",
        "agent_id":   ELEVENLABS_AGENT_ID[:12] + "..." if ELEVENLABS_AGENT_ID else "NOT SET",
        "server_url": clean(os.getenv("SERVER_URL", "NOT SET")),
    }


# ── Step 1 · Incoming call → stream to ElevenLabs agent ──────────────────────
@app.post("/incoming-call")
async def incoming_call(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "unknown")
    log.info(f"Incoming call: {call_sid}")
    session_store.create(call_sid)

    response = VoiceResponse()
    connect  = Connect()
    connect.stream(
        url=f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    )
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


# ── Step 2 · ElevenLabs agent webhook — dial the contact ─────────────────────
@app.post("/connect-parties")
async def connect_parties(request: Request):
    body = await request.json()
    log.info(f"Connect parties request: {body}")

    # Generate a clean call_sid — strip special chars like + from phone numbers
    raw      = body.get("call_sid") or body.get("contact_number", "unknown")
    call_sid = re.sub(r"[^a-zA-Z0-9]", "", raw)

    caller_lang    = body.get("caller_lang", "English")
    contact_number = body.get("contact_number")
    contact_lang   = body.get("contact_lang", "English")

    if not contact_number:
        return {"error": "contact_number is required"}

    twilio_number = clean(os.getenv("TWILIO_PHONE_NUMBER", "+18773797230"))
    server_url    = clean(os.getenv("SERVER_URL", "https://interpreter-service-production.up.railway.app"))

    session_store.update(call_sid, {
        "caller_lang":    caller_lang,
        "contact_lang":   contact_lang,
        "contact_number": contact_number,
    })

    bridge_url = f"{server_url}/bridge-answer?call_sid={call_sid}"
    log.info(f"Dialing {contact_number} from {twilio_number}")
    log.info(f"Bridge URL: {bridge_url}")
    log.info(f"Languages: {caller_lang} <-> {contact_lang}")

    twilio   = get_twilio_client()
    outbound = twilio.calls.create(
        to=contact_number,
        from_=twilio_number,
        url=bridge_url,
        status_callback=f"{server_url}/call-status",
    )

    log.info(f"Outbound call SID: {outbound.sid}")
    session_store.update(call_sid, {"outbound_sid": outbound.sid})
    return {"status": "dialing", "outbound_sid": outbound.sid}


# ── Step 3 · Contact answered — stream their audio to RTT bridge ──────────────
@app.post("/bridge-answer")
async def bridge_answer(request: Request):
    params     = request.query_params
    call_sid   = params.get("call_sid", "unknown")
    server_url = clean(os.getenv("SERVER_URL", "https://interpreter-service-production.up.railway.app"))
    ws_host    = server_url.replace("https://", "").replace("http://", "")
    ws_url     = f"wss://{ws_host}/ws/bridge/{call_sid}/contact"

    log.info(f"Bridge answer — call_sid: {call_sid}")
    log.info(f"Streaming to WebSocket: {ws_url}")

    response = VoiceResponse()
    connect  = Connect()
    connect.stream(url=ws_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


# ── Step 4 · WebSocket — RTT bridge for both parties ─────────────────────────
active_bridges: dict[str, RTTBridge] = {}


@app.websocket("/ws/bridge/{call_sid}/{party}")
async def ws_bridge(websocket: WebSocket, call_sid: str, party: str):
    await websocket.accept()
    log.info(f"WS Bridge - call_sid: {call_sid} party: {party}")

    session = session_store.get(call_sid)
    log.info(f"Session found: {session is not None} — Active sessions: {list(session_store.all().keys())}")

    # Create a fallback session if missing
    if not session:
        log.warning(f"No session for {call_sid} — creating fallback")
        session_store.create(call_sid)
        session_store.update(call_sid, {
            "caller_lang":  "English",
            "contact_lang": "Spanish",
        })
        session = session_store.get(call_sid)

    # Create RTT bridge on first party connection
    if call_sid not in active_bridges:
        active_bridges[call_sid] = RTTBridge(
            call_sid=call_sid,
            caller_lang=session.get("caller_lang", "English"),
            contact_lang=session.get("contact_lang", "Spanish"),
        )
        log.info(f"RTT Bridge created for {call_sid} — {session.get('caller_lang')} <-> {session.get('contact_lang')}")

    bridge = active_bridges[call_sid]

    try:
        if party == "caller":
            await bridge.register_caller(websocket)
        else:
            await bridge.register_contact(websocket)

        await bridge.run()

    except WebSocketDisconnect:
        log.info(f"WebSocket disconnected: {call_sid}/{party}")
    except Exception as e:
        log.error(f"Bridge error {call_sid}/{party}: {e}", exc_info=True)
    finally:
        if call_sid in active_bridges:
            await active_bridges[call_sid].cleanup()
            del active_bridges[call_sid]


# ── Call status callback ───────────────────────────────────────────────────────
@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    log.info(f"Call status: {dict(form).get('CallStatus')} — {dict(form).get('CallSid')}")
    return {"ok": True}


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
