"""
Interpreter Bridge Service
--------------------------
Caller dials in → picks language → gets connected → real-time translation both ways
Uses: ElevenLabs (STT + TTS + Agent) · Anthropic Claude (translation) · Twilio (telephony)
"""

import asyncio
import json
import logging
import os

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.agent import create_interpreter_agent
from app.bridge import InterpreterBridge
from app.translator import ClaudeTranslator
from app.session import SessionStore

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Interpreter Bridge Service")

# Singletons
translator = ClaudeTranslator()
session_store = SessionStore()

ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
SERVER_URL = os.getenv("SERVER_URL", "")


def get_twilio_client():
    """Create a fresh Twilio client reading env vars at call time."""
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    log.info(f"Twilio SID: '{sid[:8] if sid else 'EMPTY'}' Token: '{'SET' if token else 'EMPTY'}'")
    return Client(sid, token)


@app.get("/")
async def root():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "NOT SET")
    return {
        "status": "Interpreter Bridge running",
        "twilio_sid": sid[:8] + "..." if sid != "NOT SET" else "NOT SET",
        "server_url": os.getenv("SERVER_URL", "NOT SET"),
        "agent_id": ELEVENLABS_AGENT_ID[:10] + "..." if ELEVENLABS_AGENT_ID else "NOT SET",
    }


@app.post("/incoming-call")
async def incoming_call(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "unknown")
    log.info(f"Incoming call: {call_sid}")
    session_store.create(call_sid)

    response = VoiceResponse()
    connect = Connect()
    connect.stream(
        url=f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    )
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.post("/connect-parties")
async def connect_parties(request: Request):
    body = await request.json()
    log.info(f"Connect parties request: {body}")

    call_sid = body.get("call_sid") or f"xi_{body.get('contact_number','unknown')}"
    caller_lang = body.get("caller_lang", "English")
    contact_number = body.get("contact_number")
    contact_lang = body.get("contact_lang", "English")

    if not contact_number:
        return {"error": "contact_number is required"}

    session_store.update(call_sid, {
        "caller_lang":    caller_lang,
        "contact_lang":   contact_lang,
        "contact_number": contact_number,
    })

    twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "")
    server_url = os.getenv("SERVER_URL", "")

    log.info(f"Dialing {contact_number} from {twilio_number} via {server_url}")

    twilio = get_twilio_client()
    outbound = twilio.calls.create(
        to=contact_number,
        from_=twilio_number,
        url=f"{server_url}/bridge-answer?call_sid={call_sid}",
        status_callback=f"{server_url}/call-status",
    )

    log.info(f"Outbound call SID: {outbound.sid}")
    session_store.update(call_sid, {"outbound_sid": outbound.sid})
    return {"status": "dialing", "outbound_sid": outbound.sid}


@app.post("/bridge-answer")
async def bridge_answer(request: Request):
    params = request.query_params
    call_sid = params.get("call_sid")
    server_url = os.getenv("SERVER_URL", "")

    response = VoiceResponse()
    connect = Connect()
    connect.stream(
        url=f"wss://{server_url.replace('https://','').replace('http://','')}/ws/bridge/{call_sid}/contact"
    )
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


active_bridges: dict[str, InterpreterBridge] = {}

@app.websocket("/ws/bridge/{call_sid}/{party}")
async def ws_bridge(websocket: WebSocket, call_sid: str, party: str):
    await websocket.accept()
    session = session_store.get(call_sid)

    if not session:
        await websocket.close(code=1008)
        return

    if call_sid not in active_bridges:
        active_bridges[call_sid] = InterpreterBridge(
            call_sid=call_sid,
            caller_lang=session["caller_lang"],
            contact_lang=session["contact_lang"],
            translator=translator,
        )

    bridge = active_bridges[call_sid]
    try:
        if party == "caller":
            await bridge.register_caller(websocket)
        else:
            await bridge.register_contact(websocket)
        await bridge.run()
    except WebSocketDisconnect:
        log.info(f"WebSocket disconnected: {call_sid}/{party}")
    finally:
        if call_sid in active_bridges:
            del active_bridges[call_sid]


@app.post("/translate")
async def translate_endpoint(request: Request):
    body = await request.json()
    result = await translator.translate(
        text=body["text"],
        source_lang=body["source_lang"],
        target_lang=body["target_lang"],
    )
    return {"translated_text": result}


@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    log.info(f"Call status: {dict(form)}")
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)