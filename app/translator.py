"""
translator.py
─────────────
Uses Anthropic Claude (claude-sonnet-4-20250514) for real-time translation.
Keeps a small LRU cache so repeated phrases (greetings, common phrases) don't
cost extra API calls.
"""

import asyncio
import logging
import os
from collections import OrderedDict

import anthropic

log = logging.getLogger(__name__)


class LRUCache:
    def __init__(self, capacity: int = 256):
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._capacity = capacity

    def get(self, key: str) -> str | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: str):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)


class ClaudeTranslator:
    """
    Wraps the Anthropic Messages API for low-latency translation.

    Usage:
        translator = ClaudeTranslator()
        result = await translator.translate("Hola", "Spanish", "English")
        # → "Hello"
    """

    MODEL = "claude-sonnet-4-20250514"

    SYSTEM_PROMPT = """You are a professional real-time interpreter. 
Your ONLY job is to translate the user's text from {source_lang} to {target_lang}.

Rules:
- Output ONLY the translated text — no explanations, no notes, no alternatives.
- Preserve tone, emotion, and intent exactly.
- If the text is already in {target_lang}, output it unchanged.
- Never add quotation marks around your output.
- For numbers, dates, and proper nouns: keep them as-is unless they have a known local equivalent."""

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self._cache  = LRUCache(capacity=512)

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Async translation — runs the sync Anthropic client in a thread pool."""
        if not text or not text.strip():
            return text

        if source_lang.lower() == target_lang.lower():
            return text

        cache_key = f"{source_lang}|{target_lang}|{text}"
        cached = self._cache.get(cache_key)
        if cached:
            log.debug(f"Cache hit: {cache_key[:60]}")
            return cached

        result = await asyncio.to_thread(self._translate_sync, text, source_lang, target_lang)
        self._cache.put(cache_key, result)
        return result

    def _translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        system = self.SYSTEM_PROMPT.format(
            source_lang=source_lang,
            target_lang=target_lang,
        )
        try:
            message = self._client.messages.create(
                model=self.MODEL,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": text}],
            )
            translated = message.content[0].text.strip()
            log.info(f"Translated [{source_lang}→{target_lang}]: {text[:40]!r} → {translated[:40]!r}")
            return translated
        except anthropic.APIError as e:
            log.error(f"Anthropic API error: {e}")
            return text   # fail-safe: return original so call doesn't drop

    def translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        """Synchronous wrapper for non-async contexts."""
        return self._translate_sync(text, source_lang, target_lang)
