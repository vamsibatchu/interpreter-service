# Interpreter Bridge Service — Setup Guide

Real-time phone interpreter: caller speaks Language A, contact hears Language B — and vice versa.  
**Stack:** ElevenLabs (Agent + STT + TTS) · Anthropic Claude (translation) · Twilio (telephony)

---

## Architecture

```
Caller dials Twilio number
        │
        ▼
Twilio streams audio ──► ElevenLabs Agent
                                │
                        Asks: language? who?
                                │
                        Calls /connect-parties webhook
                                │
                    ┌───────────┴───────────┐
                    │                       │
             Caller stream            Twilio dials contact
                    │                       │
                    └────── Bridge ─────────┘
                                │
                    Caller speaks Spanish
                    → STT (ElevenLabs Scribe)
                    → Translate (Claude)
                    → TTS in English (ElevenLabs)
                    → Contact hears English
                    
                    Contact speaks English
                    → STT → Translate → TTS in Spanish
                    → Caller hears Spanish
```

---

## Step 1 — Accounts & API Keys

| Service | Where to get key |
|---|---|
| **Anthropic** | https://console.anthropic.com → API Keys |
| **ElevenLabs** | https://elevenlabs.io → Profile → API Keys |
| **Twilio** | https://twilio.com/console → Account SID + Auth Token |

---

## Step 2 — Twilio Setup

1. Log in to **twilio.com/console**
2. Go to **Phone Numbers → Manage → Buy a number**
3. Buy a number with **Voice** capability
4. Note the number (you'll put it in `.env` as `TWILIO_PHONE_NUMBER`)

---

## Step 3 — ElevenLabs Voice Setup

1. Go to **elevenlabs.io → Voices → Voice Library**
2. Pick a **multilingual voice** (recommended: *Rachel* or *Matilda* — both support 30+ languages)
3. Copy the **Voice ID** from the voice detail page
4. Put it in `.env` as `ELEVENLABS_VOICE_ID`

---

## Step 4 — Install & Configure

```bash
# Clone / create the project folder, then:
cd interpreter-service

# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env template
copy .env.example .env        # Windows
# cp .env.example .env        # Mac/Linux

# Fill in your keys in .env
```

---

## Step 5 — Create the ElevenLabs Agent

Run this **once** to provision the agent:

```bash
python -m app.agent
```

It will print:
```
✅ Agent created! Add this to your .env:
   ELEVENLABS_AGENT_ID=agent_xxxxxxxxxxxxxxxxx
```

Copy that ID into your `.env` file.

---

## Step 6 — Expose Your Server (ngrok)

Twilio and ElevenLabs need a public HTTPS URL to reach your server.

```bash
# Install ngrok: https://ngrok.com/download
ngrok http 8000
```

Copy the `https://xxxx.ngrok.io` URL into `.env` as `SERVER_URL`.

---

## Step 7 — Configure Twilio Webhook

1. Go to **twilio.com/console → Phone Numbers → your number**
2. Under **Voice & Fax → A Call Comes In** set:
   - **Webhook:** `https://your-ngrok-url.ngrok.io/incoming-call`
   - **HTTP Method:** `POST`
3. Save

---

## Step 8 — Run the Server

```bash
python main.py
```

You should see:
```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## Step 9 — Test It

1. **Call your Twilio number** from any phone
2. The ElevenLabs agent answers: *"What language do you speak, and who would you like to connect with?"*
3. Say e.g. *"I speak Spanish, I want to connect with John at +1 202 555 0100, he speaks English"*
4. Agent confirms and dials John
5. When John answers — everything is translated in real time, both ways

---

## ElevenLabs Dashboard — What to Check

After creating the agent you can view/edit it at:  
**elevenlabs.io → Conversational AI → Agents → Interpreter Bridge Agent**

Things to verify:
- ✅ Agent prompt is set correctly
- ✅ `connect_parties` webhook tool is listed
- ✅ Webhook URL points to your `SERVER_URL/connect-parties`
- ✅ TTS model is `eleven_turbo_v2_5`

---

## Supported Languages (32)

English, Spanish, French, German, Italian, Portuguese, Mandarin/Chinese,  
Japanese, Korean, Arabic, Hindi, Russian, Dutch, Polish, Turkish,  
Swedish, Norwegian, Danish, Finnish, Greek, Hebrew, Thai, Vietnamese,  
Indonesian, Malay, Ukrainian, Czech, Romanian, Hungarian, and more.

---

## Going to Production

| Concern | Recommendation |
|---|---|
| Session storage | Replace in-memory `SessionStore` with **Redis** |
| Hosting | **Railway**, **Render**, or **AWS App Runner** (all support WebSockets) |
| Voice quality | Switch to `eleven_multilingual_v2` for higher quality (slightly more latency) |
| Monitoring | Add **Sentry** for error tracking |
| Auth | Add a secret token to the `/connect-parties` webhook |
| Scaling | Each bridge holds 2 WebSocket connections — plan ~50 concurrent calls per instance |

---

## Troubleshooting

| Error | Fix |
|---|---|
| `401 Unauthorized` from ElevenLabs | Check `ELEVENLABS_API_KEY` in `.env` |
| `Authentication Error` from Anthropic | Check `ANTHROPIC_API_KEY` in `.env` |
| Twilio says "Application Error" | Make sure ngrok is running and `SERVER_URL` is current |
| No audio heard | Check `ELEVENLABS_VOICE_ID` is a valid multilingual voice |
| Agent doesn't call webhook | Verify webhook URL in ElevenLabs dashboard matches your `SERVER_URL` |
