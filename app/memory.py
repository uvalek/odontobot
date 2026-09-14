"""Memoria conversacional respaldada en Supabase.

Reusa la tabla `n8n_chat_histories` (formato LangChain: session_id + message jsonb)
que ya alimentaba el flujo de n8n para mantener continuidad histórica.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db import supabase

TABLE = "n8n_chat_histories"

_ROLE_TO_TYPE = {"user": "human", "assistant": "ai", "system": "system"}
_TYPE_TO_ROLE = {"human": "user", "ai": "assistant", "system": "system"}


async def load_history(chat_id: str, limit: int | None = None) -> list[dict[str, str]]:
    n = limit or get_settings().memory_turns
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table(TABLE)
            .select("message")
            .eq("session_id", chat_id)
            .order("id", desc=True)
            .limit(n)
            .execute()
        )
    )
    rows = list(reversed(res.data or []))
    history: list[dict[str, str]] = []
    for r in rows:
        msg = r.get("message") or {}
        mtype = msg.get("type")
        content = (msg.get("data") or {}).get("content") or msg.get("content") or ""
        role = _TYPE_TO_ROLE.get(mtype)
        if role and content:
            history.append({"role": role, "content": content})
    return history


async def append(
    chat_id: str,
    role: str,
    content: str,
    *,
    metadata: dict | None = None,
) -> None:
    """Guarda un mensaje en n8n_chat_histories.

    `metadata` se mete en `data.additional_kwargs` y permite distinguir
    mensajes manuales del personal de la clinica (`{"sender": "advisor", "advisor_name": "Ana"}`)
    de los del bot. El dashboard lo lee para pintar la etiqueta correcta.
    """
    if not content:
        return
    mtype = _ROLE_TO_TYPE.get(role, "human")
    payload = {
        "session_id": chat_id,
        "message": {
            "type": mtype,
            "data": {
                "content": content,
                "additional_kwargs": metadata or {},
                "response_metadata": {},
            },
        },
    }
    await asyncio.to_thread(
        lambda: supabase().table(TABLE).insert(payload).execute()
    )
