"""Transcripción de audio (Whisper) y descripción de imágenes (Vision)."""

from __future__ import annotations

import asyncio
import io

import httpx
from openai import OpenAI

from app.config import get_settings, load_prompt
from app.llm import completion_params

_VISION_PROMPT = load_prompt("vision")


def _client() -> OpenAI:
    s = get_settings()
    return OpenAI(
        api_key=s.openai_api_key, timeout=s.openai_timeout_seconds, max_retries=s.openai_max_retries
    )


async def transcribe_audio(url: str, *, headers: dict[str, str] | None = None) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.get(url, headers=headers or {})
        r.raise_for_status()
        audio = r.content

    def _do() -> str:
        buf = io.BytesIO(audio)
        buf.name = "audio.ogg"
        resp = _client().audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="es",
        )
        return resp.text

    return await asyncio.to_thread(_do)


async def describe_image(url: str) -> str:
    settings = get_settings()

    def _do() -> str:
        resp = _client().chat.completions.create(
            model=settings.openai_model_vision,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                    ],
                }
            ],
            **completion_params(settings.openai_model_vision),
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_do)
