"""
agent.py
────────
Creates (or retrieves) the ElevenLabs Conversational AI agent that:
  1. Greets the caller
  2. Asks what language they speak
  3. Asks who they want to connect with + their number
  4. Calls the /connect-parties webhook to trigger the outbound call + bridge

Run once to provision the agent:
    python -m app.agent
"""

import json
import logging
import os

from elevenlabs.client import ElevenLabs
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


AGENT_SYSTEM_PROMPT = """You are a friendly, professional interpreter services operator.

Your job is to:
1. Greet the caller warmly.
2. Ask what language they speak (detect from their speech if possible).
3. Ask who they would like to connect with and that person's phone number.
4. Ask what language the person they're connecting with speaks.
5. Confirm the details back to the caller.
6. Use the 'connect_parties' tool to initiate the connection.
7. Let the caller know they are being connected and to hold briefly.

Keep responses short and clear. Be warm but efficient — callers want to be connected quickly.
Always confirm the phone number digit by digit to avoid errors."""


CONNECT_TOOL = {
    "name": "connect_parties",
    "description": (
        "Call this once you have collected: the caller's language, "
        "the contact's name, the contact's phone number, and the contact's language. "
        "This will dial the contact and set up the real-time interpreter bridge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "call_sid": {
                "type": "string",
                "description": "The Twilio Call SID — injected automatically from the call metadata variable {{call_sid}}",
            },
            "caller_lang": {
                "type": "string",
                "description": "Full language name the caller speaks, e.g. 'Spanish', 'Mandarin', 'French'",
            },
            "contact_name": {
                "type": "string",
                "description": "Name of the person the caller wants to reach",
            },
            "contact_number": {
                "type": "string",
                "description": "E.164 phone number of the contact, e.g. +12025551234",
            },
            "contact_lang": {
                "type": "string",
                "description": "Full language name the contact speaks, e.g. 'English'",
            },
        },
        "required": ["call_sid", "caller_lang", "contact_number", "contact_lang"],
    },
}


def create_interpreter_agent(server_url: str | None = None) -> str:
    """
    Creates the ElevenLabs Conversational AI agent and returns its agent_id.
    If ELEVENLABS_AGENT_ID is already set in env, returns that instead.
    """
    existing = os.getenv("ELEVENLABS_AGENT_ID")
    if existing:
        log.info(f"Using existing agent: {existing}")
        return existing

    url = server_url or os.getenv("SERVER_URL", "https://yourserver.com")
    client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

    agent_config = {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": AGENT_SYSTEM_PROMPT,
                    "tools": [
                        {
                            "type": "webhook",
                            "name": CONNECT_TOOL["name"],
                            "description": CONNECT_TOOL["description"],
                            "api_schema": {
                                "url": f"{url}/connect-parties",
                                "method": "POST",
                                "request_body_schema": {
                                    "type": "object",
                                    "properties": CONNECT_TOOL["parameters"]["properties"],
                                    "required": CONNECT_TOOL["parameters"]["required"],
                                },
                            },
                        }
                    ],
                },
                "first_message": (
                    "Thank you for calling interpreter services. "
                    "What language do you speak, and who would you like to be connected with today?"
                ),
                "language": "en",
            },
            "tts": {
                "voice_id": os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
                "model_id": "eleven_turbo_v2_5",   # lowest latency
                "optimize_streaming_latency": 4,
            },
            "stt": {
                "quality": "high",
            },
            "turn": {
                "mode": "server_vad",   # server-side VAD for telephony
            },
        },
        "name": "Interpreter Bridge Agent",
    }

    response = client.conversational_ai.create_agent(**agent_config)
    agent_id = response.agent_id
    log.info(f"Created ElevenLabs agent: {agent_id}")
    print(f"\n✅ Agent created! Add this to your .env:\n   ELEVENLABS_AGENT_ID={agent_id}\n")
    return agent_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_interpreter_agent()
