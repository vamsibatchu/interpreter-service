"""
bridge.py
─────────
InterpreterBridge — handles two simultaneous Twilio Media Streams (caller + contact),
intercepts audio, runs STT → Claude translation → TTS in both directions.

Twilio sends audio as base64-encoded µ-law 8kHz chunks over WebSocket.
ElevenLabs TTS returns the same format when output_format="ulaw_8000".
"""

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field

from elevenlabs.client import ElevenLabs
from fastapi import WebSocket

from app.translator import ClaudeTranslator

log = logging.getLogger(__name__)

XI_CLIENT = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
VOICE_ID   = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")


@dataclass
class InterpreterBridge:
    call_sid:     str
    caller_lang:  str
    contact_lang: str
    translator:   ClaudeTranslator

    _caller_ws:  WebSocket | None = field(default=None, init=False)
    _contact_ws: WebSocket | None = field(default=None, init=False)
    _ready:      asyncio.Event    = field(default_factory=asyncio.Event, init=False)

    # ── Registration ──────────────────────────────────────────────────────────
    async def register_caller(self, ws: WebSocket):
        self._caller_ws = ws
        log.info(f"[{self.call_sid}] Caller WebSocket registered ({self.caller_lang})")
        self._check_ready()

    async def register_contact(self, ws: WebSocket):
        self._contact_ws = ws
        log.info(f"[{self.call_sid}] Contact WebSocket registered ({self.contact_lang})")
        self._check_ready()

    def _check_ready(self):
        if self._caller_ws and self._contact_ws:
            self._ready.set()

    # ── Main loop ─────────────────────────────────────────────────────────────
    async def run(self):
        """Wait for both parties then stream in both directions simultaneously."""
        await asyncio.wait_for(self._ready.wait(), timeout=30.0)
        log.info(f"[{self.call_sid}] Bridge active — translating {self.caller_lang} ↔ {self.contact_lang}")

        await asyncio.gather(
            self._stream(
                src_ws=self._caller_ws,
                dst_ws=self._contact_ws,
                src_lang=self.caller_lang,
                dst_lang=self.contact_lang,
                label="caller→contact",
            ),
            self._stream(
                src_ws=self._contact_ws,
                dst_ws=self._caller_ws,
                src_lang=self.contact_lang,
                dst_lang=self.caller_lang,
                label="contact→caller",
            ),
        )

    # ── Per-direction stream processor ────────────────────────────────────────
    async def _stream(
        self,
        src_ws: WebSocket,
        dst_ws: WebSocket,
        src_lang: str,
        dst_lang: str,
        label: str,
    ):
        """
        Reads Twilio media messages from src_ws, accumulates speech,
        translates via Claude, synthesises via ElevenLabs, sends to dst_ws.

        Twilio message types we care about:
          • "media"   – audio chunk (base64 µ-law)
          • "mark"    – speech boundary signal
          • "stop"    – stream ended
        """
        speech_buffer: list[str] = []   # base64 chunks between silence gaps

        async for raw in src_ws.iter_text():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = msg.get("event")

            if event == "media":
                # Accumulate raw audio chunks
                speech_buffer.append(msg["media"]["payload"])

            elif event == "mark":
                # Twilio fires "mark" at natural speech boundaries — good moment to translate
                if speech_buffer:
                    audio_b64 = "".join(speech_buffer)
                    speech_buffer.clear()
                    asyncio.create_task(
                        self._translate_and_forward(
                            audio_b64=audio_b64,
                            dst_ws=dst_ws,
                            src_lang=src_lang,
                            dst_lang=dst_lang,
                            stream_sid=msg.get("streamSid", ""),
                            label=label,
                        )
                    )

            elif event == "stop":
                log.info(f"[{self.call_sid}] Stream stopped: {label}")
                # Flush any remaining audio
                if speech_buffer:
                    audio_b64 = "".join(speech_buffer)
                    await self._translate_and_forward(
                        audio_b64=audio_b64,
                        dst_ws=dst_ws,
                        src_lang=src_lang,
                        dst_lang=dst_lang,
                        stream_sid="",
                        label=label,
                    )
                break

    # ── Translate + synthesise + forward ─────────────────────────────────────
    async def _translate_and_forward(
        self,
        audio_b64: str,
        dst_ws: WebSocket,
        src_lang: str,
        dst_lang: str,
        stream_sid: str,
        label: str,
    ):
        """
        1. STT  – ElevenLabs speech_to_text (async)
        2. Translate – Claude
        3. TTS  – ElevenLabs TTS → µ-law 8kHz
        4. Send – Twilio media message back to dst WebSocket
        """
        try:
            # 1 ▸ Speech-to-Text
            audio_bytes = base64.b64decode(audio_b64)
            stt_response = await asyncio.to_thread(
                XI_CLIENT.speech_to_text.convert,
                audio=audio_bytes,
                model_id="scribe_v1",
                language_code=_lang_to_code(src_lang),
            )
            text = stt_response.text.strip()
            if not text:
                return

            log.info(f"[{label}] STT: {text[:80]!r}")

            # 2 ▸ Translate via Claude
            translated = await self.translator.translate(text, src_lang, dst_lang)
            log.info(f"[{label}] Translated: {translated[:80]!r}")

            # 3 ▸ Text-to-Speech
            audio_gen = await asyncio.to_thread(
                XI_CLIENT.text_to_speech.convert,
                text=translated,
                voice_id=VOICE_ID,
                model_id="eleven_turbo_v2_5",
                output_format="ulaw_8000",   # Twilio's required format
            )
            translated_audio = b"".join(audio_gen)

            # 4 ▸ Send to destination WebSocket as Twilio media message
            payload = base64.b64encode(translated_audio).decode("utf-8")
            await dst_ws.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload},
            }))

        except Exception as e:
            log.error(f"[{label}] Bridge error: {e}", exc_info=True)


# ── Helper: language name → ISO 639-1 code ───────────────────────────────────
_LANG_MAP = {
    "english":    "en",  "spanish":   "es",  "french":    "fr",
    "german":     "de",  "italian":   "it",  "portuguese":"pt",
    "mandarin":   "zh",  "chinese":   "zh",  "japanese":  "ja",
    "korean":     "ko",  "arabic":    "ar",  "hindi":     "hi",
    "russian":    "ru",  "dutch":     "nl",  "polish":    "pl",
    "turkish":    "tr",  "swedish":   "sv",  "norwegian": "no",
    "danish":     "da",  "finnish":   "fi",  "greek":     "el",
    "hebrew":     "he",  "thai":      "th",  "vietnamese":"vi",
    "indonesian": "id",  "malay":     "ms",  "ukrainian": "uk",
    "czech":      "cs",  "romanian":  "ro",  "hungarian": "hu",
}

def _lang_to_code(language: str) -> str:
    return _LANG_MAP.get(language.lower(), "en")
