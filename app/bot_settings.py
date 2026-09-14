"""Toggle del bot por conversacion (chat_id).

Tabla: `bot_settings(chat_id PK, channel, bot_enabled, last_read_at, updated_at)`.

- `is_enabled(chat_id)` devuelve True por default si no hay fila (asi el bot
  responde a contactos nuevos hasta que el personal lo apague desde el dashboard o se active el handoff).
- `set_enabled(chat_id, enabled, channel)` upsert.
- `mark_read(chat_id)` actualiza last_read_at a NOW() — usado por el dashboard
  cuando el personal de la clinica abre la conversacion.
- `unread_count(chat_id)` cuenta mensajes humanos despues del last_read_at.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from app.db import supabase

log = structlog.get_logger(__name__)

TABLE = "bot_settings"


async def is_enabled(chat_id: str) -> bool:
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table(TABLE)
            .select("bot_enabled")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
    )
    rows = res.data or []
    if not rows:
        return True  # default ON para conversaciones nuevas
    return bool(rows[0].get("bot_enabled", True))


async def ensure_row(chat_id: str, channel: str) -> None:
    """Inserta una fila con defaults si no existe. Idempotente."""
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table(TABLE)
            .select("chat_id")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
    )
    if res.data:
        return
    payload = {
        "chat_id": chat_id,
        "channel": channel,
        "bot_enabled": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await asyncio.to_thread(
        lambda: supabase().table(TABLE).insert(payload).execute()
    )


async def set_enabled(chat_id: str, enabled: bool, channel: str | None = None) -> dict:
    payload = {
        "chat_id": chat_id,
        "bot_enabled": enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if channel:
        payload["channel"] = channel
    res = await asyncio.to_thread(
        lambda: supabase().table(TABLE).upsert(payload, on_conflict="chat_id").execute()
    )
    return (res.data or [{}])[0]


async def mark_read(chat_id: str) -> None:
    # UPDATE en lugar de upsert: la columna `channel` es NOT NULL y al hacer
    # upsert sin canal Postgres construye la fila a insertar antes de detectar
    # el conflicto y revienta. Si no existe la fila, no hay nada que marcar.
    payload = {
        "last_read_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await asyncio.to_thread(
        lambda: supabase().table(TABLE).update(payload).eq("chat_id", chat_id).execute()
    )
