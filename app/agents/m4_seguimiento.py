"""M4 — Seguimiento de leads que ya tuvieron contacto."""

from __future__ import annotations

import asyncio

from openai import OpenAI

from app.config import get_settings
from app.llm import completion_params
from app.security.system_prompt import secure_system_prompt

_SYSTEM = secure_system_prompt("m4_seguimiento")


def _client() -> OpenAI:
    s = get_settings()
    return OpenAI(
        api_key=s.openai_api_key, timeout=s.openai_timeout_seconds, max_retries=s.openai_max_retries
    )


async def respond(user_text: str, history: list[dict[str, str]]) -> str:
    s = get_settings()
    msgs: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM}]
    msgs.extend(history[-25:])
    msgs.append({"role": "user", "content": user_text})

    def _do() -> str:
        resp = _client().chat.completions.create(
            model=s.openai_model_brain,
            messages=msgs,  # type: ignore[arg-type]
            **completion_params(s.openai_model_brain, temperature=0.5),
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_do)
