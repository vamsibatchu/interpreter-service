"""
rtt_bridge.py
─────────────
Real-time translation bridge using ElevenLabs RTT WebSocket API.

Architecture:
  Caller audio (Twilio ulaw_8000) → RTT API → Translated audio → Contact
  Contact audio (Twilio ulaw_8000) → RTT API → Translated audio → Caller

Each direction gets its own RTT WebSocket session.
"""

import asyncio
import json
import logging
import os

import websockets
from fastapi import WebSocket

log = logging.getLogger(__name__)

RTT_ENDPOINT = "wss://api.elevenlabs.io/v1/realtime-translation"
XI_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
VOICE_ID     = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Language name to ISO 639-1 code
_LANG_MAP = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "mandarin": "zh", "chinese": "zh",
    "japanese": "ja", "korean": "ko", "arabic": "ar", "hindi": "hi",
    "russian": "ru", "dutch": "nl", "polish": "pl", "turkish": "tr",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "greek": "el", "hebrew": "he", "thai": "th", "vietnamese": "vi",
    "indonesian": "id", "malay": "ms", "ukrainian": "uk",
}

def lang_code(name: str) -> str:
    return _LANG_MAP.get(name.lower().strip(), "en")


class RTTSession:
    """
    One ElevenLabs RTT WebSocket session for one direction of translation.
    Receives Twilio ulaw_8000 audio, sends to RTT, returns translated audio.
    """

    def __init__(self, src_lang: str, dst_lang: str, label: str):
        self.src_lang      = lang_code(src_lang)
        self.dst_lang      = lang_code(dst_lang)
        self.label         = label
        self._ws           = None
        self._output_queue = asyncio.Queue()

    async def connect(self):
        url = (
            f"{RTT_ENDPOINT}"
            f"?target_language={self.dst_lang}"
            f"&source_language={self.src_lang}"
            f"&voice_id={VOICE_ID}"
            f"&input_format=ulaw_8000"
            f"&output_format=ulaw_8000"
        )
        headers = {"xi-api-key": XI_API_KEY}
        self._ws = await websockets.connect(url, additional_headers=headers)
        log.info(f"[{self.label}] RTT connected ({self.src_lang}→{self.dst_lang})")

    async def send_audio(self, base64_audio: str):
        if self._ws:
            await self._ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "data": base64_audio,
            }))

    async def end_stream(self):
        if self._ws:
            try:
                await self._ws.send(json.dumps({"message_type": "end_of_stream"}))
            except Exception:
                pass

    async def receive_loop(self):
        """Read from RTT and queue translated audio chunks."""
        try:
            async for raw in self._ws:
                msg   = json.loads(raw)
                mtype = msg.get("message_type", "")

                if mtype == "session_started":
                    log.info(f"[{self.label}] Session ID: {msg.get('session_id')}")

                elif mtype == "final_transcript":
                    log.info(f"[{self.label}] Transcript: {msg.get('text','')[:80]}")

                elif mtype == "translation":
                    log.info(f"[{self.label}] Translation: {msg.get('text','')[:80]}")

                elif mtype == "audio":
                    await self._output_queue.put(msg.get("data", ""))

                elif mtype == "status":
                    log.info(f"[{self.label}] Status: {msg.get('status')}")

                elif mtype == "error":
                    log.error(f"[{self.label}] RTT error: {msg.get('error')}")

        except Exception as e:
            log.info(f"[{self.label}] RTT receive loop ended: {e}")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


class RTTBridge:
    """
    Bidirectional real-time translation bridge between two Twilio call legs.

    caller_ws  → RTT(caller_lang → contact_lang) → contact_ws
    contact_ws → RTT(contact_lang → caller_lang) → caller_ws
    """

    def __init__(self, call_sid: str, caller_lang: str, contact_lang: str):
        self.call_sid     = call_sid
        self.caller_lang  = caller_lang
        self.contact_lang = contact_lang
        self._caller_ws   = None
        self._contact_ws  = None

        self._caller_rtt  = RTTSession(caller_lang, contact_lang, f"{call_sid}/caller→contact")
        self._contact_rtt = RTTSession(contact_lang, caller_lang, f"{call_sid}/contact→caller")

    async def register_caller(self, ws: WebSocket):
        self._caller_ws = ws
        log.info(f"[{self.call_sid}] Caller WebSocket registered ({self.caller_lang})")

    async def register_contact(self, ws: WebSocket):
        self._contact_ws = ws
        log.info(f"[{self.call_sid}] Contact WebSocket registered ({self.contact_lang})")

    async def run(self):
        log.info(f"[{self.call_sid}] Connecting RTT sessions...")

        try:
            await self._caller_rtt.connect()
            log.info(f"[{self.call_sid}] Caller RTT connected")
        except Exception as e:
            log.error(f"[{self.call_sid}] Failed to connect caller RTT: {e}")
            return
        
        try:
            await self._contact_rtt.connect()
            log.info(f"[{self.call_sid}] Contact RTT connected")
        except Exception as e:
            log.error(f"[{self.call_sid}] Failed to connect contact RTT: {e}")
            return

        log.info(f"[{self.call_sid}] RTT bridge active — {self.caller_lang} <-> {self.contact_lang}")

        tasks = [
            # RTT receive loops
            asyncio.create_task(self._caller_rtt.receive_loop()),
            asyncio.create_task(self._contact_rtt.receive_loop()),
        ]

        if self._caller_ws:
            # Caller audio → RTT → Contact
            tasks.append(asyncio.create_task(
                self._twilio_to_rtt(self._caller_ws, self._caller_rtt, "caller")
            ))
            tasks.append(asyncio.create_task(
                self._rtt_to_twilio(self._contact_rtt, self._caller_ws, "to_caller")
            ))

        if self._contact_ws:
            # Contact audio → RTT → Caller
            tasks.append(asyncio.create_task(
                self._twilio_to_rtt(self._contact_ws, self._contact_rtt, "contact")
            ))
            tasks.append(asyncio.create_task(
                self._rtt_to_twilio(self._caller_rtt, self._contact_ws, "to_contact")
            ))

        await asyncio.gather(*tasks, return_exceptions=True)
        await self.cleanup()
        log.info(f"[{self.call_sid}] RTT bridge ended")

    async def _twilio_to_rtt(self, twilio_ws: WebSocket, rtt: RTTSession, label: str):
        """Read Twilio media stream and forward audio to RTT API."""
        try:
            async for raw in twilio_ws.iter_text():
                try:
                    msg   = json.loads(raw)
                    event = msg.get("event", "")

                    if event == "connected":
                        log.info(f"[{self.call_sid}] [{label}] Twilio connected")

                    elif event == "start":
                        log.info(f"[{self.call_sid}] [{label}] Stream started: {msg.get('streamSid','')}")

                    elif event == "media":
                        await rtt.send_audio(msg["media"]["payload"])

                    elif event == "stop":
                        log.info(f"[{self.call_sid}] [{label}] Stream stopped")
                        await rtt.end_stream()
                        break

                except (json.JSONDecodeError, KeyError):
                    pass

        except Exception as e:
            log.info(f"[{self.call_sid}] [{label}] Ended: {e}")
            await rtt.end_stream()

    async def _rtt_to_twilio(self, rtt: RTTSession, twilio_ws: WebSocket, label: str):
        """Forward translated audio from RTT output queue to Twilio."""
        try:
            while True:
                audio_b64 = await rtt._output_queue.get()
                if audio_b64:
                    await twilio_ws.send_text(json.dumps({
                        "event": "media",
                        "media": {"payload": audio_b64},
                    }))
                    log.debug(f"[{self.call_sid}] [{label}] Sent translated audio")
        except Exception as e:
            log.info(f"[{self.call_sid}] [{label}] Ended: {e}")

    async def cleanup(self):
        await asyncio.gather(
            self._caller_rtt.close(),
            self._contact_rtt.close(),
            return_exceptions=True,
        )
