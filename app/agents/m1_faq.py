"""M1 — Información general de la clínica (FAQ) con RAG opcional sobre Supabase.

Usa el mismo `match_documents` RPC y tabla `documents` que ya existe en
Supabase desde el flujo n8n original.
"""

from __future__ import annotations

import asyncio

import httpx
from openai import OpenAI

from app.config import get_settings
from app.llm import completion_params
from app.security.system_prompt import secure_system_prompt

_SYSTEM = secure_system_prompt("m1_faq")
_TOP_K = 4


def _client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


async def _embed(text: str) -> list[float]:
    def _do() -> list[float]:
        resp = _client().embeddings.create(model="text-embedding-3-small", input=text)
        return resp.data[0].embedding

    return await asyncio.to_thread(_do)


async def _retrieve(query: str) -> str:
    s = get_settings()
    embedding = await _embed(query)
    url = f"{s.supabase_url}/rest/v1/rpc/{s.supabase_rag_query}"
    headers = {
        "apikey": s.supabase_service_key,
        "Authorization": f"Bearer {s.supabase_service_key}",
        "Content-Type": "application/json",
    }
    payload = {"query_embedding": embedding, "match_count": _TOP_K}
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            return ""
        rows = r.json() or []
    parts = []
    for row in rows:
        content = row.get("content") or row.get("page_content") or ""
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts)


async def respond(user_text: str, history: list[dict[str, str]]) -> str:
    s = get_settings()
    context = await _retrieve(user_text)
    sys = _SYSTEM
    if context:
        sys += f"\n\n<clinicKnowledge>\n{context}\n</clinicKnowledge>"
    msgs: list[dict[str, str]] = [{"role": "system", "content": sys}]
    msgs.extend(history[-15:])
    msgs.append({"role": "user", "content": user_text})

    def _do() -> str:
        resp = _client().chat.completions.create(
            model=s.openai_model_brain,
            messages=msgs,  # type: ignore[arg-type]
            **completion_params(s.openai_model_brain, temperature=0.7),
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_do)
