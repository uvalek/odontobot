"""API REST consumida por el dashboard CRM (Vite + React en Vercel).

Auth: header `X-API-Key` debe coincidir con `DASHBOARD_API_KEY`.

Endpoints (prefijo /api):
    GET    /conversations                  -> lista para la columna izquierda
    GET    /conversations/{chat_id}        -> detalle (panel derecho)
    GET    /conversations/{chat_id}/messages?limit=&before_id=
    PATCH  /conversations/{chat_id}        -> bot_enabled, notas, etapa, etc.
    POST   /conversations/{chat_id}/send   -> mensaje manual del personal de la clinica
    POST   /conversations/{chat_id}/read   -> marca como leida
    GET    /options                        -> dropdowns dinamicos del CRM

Convenciones:
- chat_id es siempre el subscriber_id de ManyChat (whatsapp/ig/messenger) o el
  chat_id de Telegram. Es la PK conversacional.
- canal interno: 'manychat' | 'telegram'. canal visible: 'whatsapp' | 'instagram'
  | 'messenger' | 'telegram'. La distincion la hace el dashboard a partir del
  campo `canal` de contactos cuando existe; si no, asumimos 'whatsapp'.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app import bot_settings, memory
from app.channels import manychat as manychat_chan
from app.channels import telegram as telegram_chan
from app.config import get_settings
from app.db import supabase

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])


# ---------- Auth ----------


def _check_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = get_settings().dashboard_api_key
    if not expected:
        raise HTTPException(status_code=503, detail="DASHBOARD_API_KEY no configurado")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="API key invalido")


ApiAuth = Depends(_check_api_key)


# ---------- Helpers ----------


def _msg_meta(message: dict) -> dict:
    """Devuelve sender, content y advisor_name a partir del jsonb LangChain."""
    if not isinstance(message, dict):
        return {"sender": "user", "content": "", "advisor_name": None}
    mtype = message.get("type") or ""
    data = message.get("data") or {}
    content = data.get("content") or message.get("content") or ""
    extras = data.get("additional_kwargs") or {}
    if mtype == "human":
        sender = "user"
    elif extras.get("sender") == "advisor":
        sender = "advisor"
    else:
        sender = "bot"
    return {
        "sender": sender,
        "content": content,
        "advisor_name": extras.get("advisor_name"),
    }


async def _list_session_ids() -> list[str]:
    """Devuelve session_ids distintos en n8n_chat_histories.
    No hay DISTINCT en supabase-py, asi que tiramos un SELECT plano y reducimos.
    """
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("n8n_chat_histories")
            .select("session_id")
            .order("id", desc=True)
            .limit(2000)
            .execute()
        )
    )
    seen: list[str] = []
    seen_set: set[str] = set()
    for r in res.data or []:
        sid = str(r.get("session_id") or "")
        if sid and sid not in seen_set:
            seen.append(sid)
            seen_set.add(sid)
    return seen


async def _last_message_for(session_id: str) -> dict | None:
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("n8n_chat_histories")
            .select("id, message, created_at")
            .eq("session_id", session_id)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
    )
    rows = res.data or []
    return rows[0] if rows else None


async def _bot_settings_map(chat_ids: list[str]) -> dict[str, dict]:
    if not chat_ids:
        return {}
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("bot_settings")
            .select("chat_id, channel, bot_enabled, last_read_at")
            .in_("chat_id", chat_ids)
            .execute()
        )
    )
    return {r["chat_id"]: r for r in (res.data or [])}


async def _contactos_map(chat_ids: list[str]) -> dict[str, dict]:
    if not chat_ids:
        return {}
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .select("*")
            .in_("chat_id", chat_ids)
            .execute()
        )
    )
    return {r["chat_id"]: r for r in (res.data or []) if r.get("chat_id")}


async def _unread_count(session_id: str, last_read_at: str | None) -> int:
    q = (
        supabase()
        .table("n8n_chat_histories")
        .select("id", count="exact")
        .eq("session_id", session_id)
    )
    if last_read_at:
        q = q.gt("created_at", last_read_at)
    res = await asyncio.to_thread(lambda: q.execute())
    return res.count or 0


# ---------- Schemas ----------


class ConversationPatch(BaseModel):
    bot_enabled: Optional[bool] = None
    notas_internas: Optional[str] = None
    etapa_seguimiento: Optional[str] = None
    # DEMO dental — columnas reutilizadas (ver app/tools/contactos.py::LEAD_COLUMNS):
    # asesor_asignado = doctor asignado, zona_interes = motivo de consulta,
    # tipo_credito = forma de pago, presupuesto_max = edad del paciente.
    asesor_asignado: Optional[str] = None
    zona_interes: Optional[str] = None
    tipo_credito: Optional[str] = None
    presupuesto_max: Optional[float] = None
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    correo: Optional[str] = None


class SendMessageBody(BaseModel):
    text: str
    advisor_name: Optional[str] = None


# ---------- Endpoints ----------


@router.get("/conversations", dependencies=[ApiAuth])
async def list_conversations(
    search: Optional[str] = None,
    channel: Optional[str] = Query(None, description="whatsapp|instagram|messenger|telegram"),
    filter: Optional[str] = Query(None, description="all|botoff|unread"),
    limit: int = 100,
) -> dict[str, Any]:
    session_ids = await _list_session_ids()
    settings_map = await _bot_settings_map(session_ids)
    contactos_map = await _contactos_map(session_ids)

    items: list[dict] = []
    # Una llamada por sesion para el ultimo mensaje. OK para <200 chats; si
    # crece, mover a una vista materializada.
    last_msgs = await asyncio.gather(*[_last_message_for(sid) for sid in session_ids])

    for sid, last in zip(session_ids, last_msgs):
        if not last:
            continue
        bs = settings_map.get(sid) or {}
        co = contactos_map.get(sid) or {}
        meta = _msg_meta(last.get("message") or {})

        ch_internal = bs.get("channel") or co.get("canal") or "manychat"
        ch_visible = co.get("canal") or ("whatsapp" if ch_internal == "manychat" else ch_internal)

        # Display name preference: nombre real (M2/personal) > handle auto > chat_id.
        display = co.get("nombre") or co.get("handle") or sid
        item = {
            "chat_id": sid,
            "channel": ch_visible,
            "channel_internal": ch_internal,
            "bot_enabled": bs.get("bot_enabled", True),
            "name": display,
            "handle": co.get("handle"),
            "telefono": co.get("telefono"),
            "correo": co.get("correo"),
            "etapa_seguimiento": co.get("etapa_seguimiento"),
            "zona_interes": co.get("zona_interes"),
            "presupuesto_max": co.get("presupuesto_max"),
            "tipo_credito": co.get("tipo_credito"),
            "asesor_asignado": co.get("asesor_asignado"),
            "last_message": meta["content"][:240],
            "last_sender": meta["sender"],
            "last_at": last.get("created_at"),
            "unread_count": await _unread_count(sid, bs.get("last_read_at")),
        }
        items.append(item)

    # Filtros (servidor — el dashboard tambien puede filtrar local sobre la lista)
    if search:
        s = search.lower()
        items = [
            i for i in items
            if s in (i["name"] or "").lower()
            or s in (i.get("telefono") or "")
            or s in (i.get("handle") or "").lower()
        ]
    if channel:
        items = [i for i in items if i["channel"] == channel]
    if filter == "botoff":
        items = [i for i in items if not i["bot_enabled"]]
    elif filter == "unread":
        items = [i for i in items if i["unread_count"] > 0]

    # Mas reciente primero
    items.sort(key=lambda x: x["last_at"] or "", reverse=True)
    return {"items": items[:limit], "total": len(items)}


@router.get("/conversations/{chat_id}", dependencies=[ApiAuth])
async def get_conversation(chat_id: str) -> dict[str, Any]:
    bs_map = await _bot_settings_map([chat_id])
    co_map = await _contactos_map([chat_id])
    last = await _last_message_for(chat_id)
    bs = bs_map.get(chat_id) or {}
    co = co_map.get(chat_id) or {}

    ch_internal = bs.get("channel") or co.get("canal") or "manychat"
    ch_visible = co.get("canal") or ("whatsapp" if ch_internal == "manychat" else ch_internal)

    return {
        "chat_id": chat_id,
        "channel": ch_visible,
        "channel_internal": ch_internal,
        "bot_enabled": bs.get("bot_enabled", True),
        "last_read_at": bs.get("last_read_at"),
        "contacto": co or None,
        "last_message_at": (last or {}).get("created_at"),
    }


@router.get("/conversations/{chat_id}/messages", dependencies=[ApiAuth])
async def list_messages(
    chat_id: str,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> dict[str, Any]:
    q = (
        supabase()
        .table("n8n_chat_histories")
        .select("id, message, created_at")
        .eq("session_id", chat_id)
        .order("id", desc=True)
        .limit(limit)
    )
    if before_id:
        q = q.lt("id", before_id)
    res = await asyncio.to_thread(lambda: q.execute())
    rows = list(reversed(res.data or []))
    out = []
    for r in rows:
        meta = _msg_meta(r.get("message") or {})
        out.append({
            "id": r["id"],
            "sender": meta["sender"],
            "advisor_name": meta["advisor_name"],
            "content": meta["content"],
            "created_at": r.get("created_at"),
        })
    return {"items": out, "chat_id": chat_id}


@router.patch("/conversations/{chat_id}", dependencies=[ApiAuth])
async def patch_conversation(chat_id: str, body: ConversationPatch) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)

    # bot_enabled vive en bot_settings (no en contactos)
    if "bot_enabled" in updates:
        # Detecta channel desde bot_settings o contactos
        bs_map = await _bot_settings_map([chat_id])
        co_map = await _contactos_map([chat_id])
        ch = (
            (bs_map.get(chat_id) or {}).get("channel")
            or (co_map.get(chat_id) or {}).get("canal")
            or "manychat"
        )
        await bot_settings.set_enabled(chat_id, updates.pop("bot_enabled"), channel=ch)

    # El resto va a contactos. Si no existe contacto pero hay datos a actualizar,
    # lo creamos minimo (solo con chat_id) para que el upsert futuro lo encuentre.
    if updates:
        existing = await asyncio.to_thread(
            lambda: (
                supabase()
                .table("contactos")
                .select("id")
                .eq("chat_id", chat_id)
                .limit(1)
                .execute()
            )
        )
        if existing.data:
            cid = existing.data[0]["id"]
            await asyncio.to_thread(
                lambda: (
                    supabase()
                    .table("contactos")
                    .update(updates)
                    .eq("id", cid)
                    .execute()
                )
            )
        else:
            payload = {"chat_id": chat_id, "nombre": updates.get("nombre") or "Sin nombre", **updates}
            await asyncio.to_thread(
                lambda: supabase().table("contactos").insert(payload).execute()
            )

    return {"ok": True, "chat_id": chat_id}


@router.post("/conversations/{chat_id}/send", dependencies=[ApiAuth])
async def send_message(chat_id: str, body: SendMessageBody) -> dict[str, Any]:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text vacio")

    # Detecta canal interno y sub-canal visible
    bs_map = await _bot_settings_map([chat_id])
    co_map = await _contactos_map([chat_id])
    bs_channel = (bs_map.get(chat_id) or {}).get("channel")
    canal_visible = (co_map.get(chat_id) or {}).get("canal")

    channel_internal = bs_channel or (
        "telegram" if canal_visible == "telegram" else "manychat"
    )
    if channel_internal in ("whatsapp", "instagram", "messenger"):
        channel_internal = "manychat"  # todos pasan por la API de ManyChat

    # Sub-canal para ManyChat: prioriza el guardado en contactos, default whatsapp
    subchannel = canal_visible if canal_visible in ("whatsapp", "instagram", "messenger") else "whatsapp"

    # Envio
    try:
        if channel_internal == "telegram":
            await telegram_chan.send_messages(chat_id, [text])
        else:
            await manychat_chan.send_messages(chat_id, [text], subchannel=subchannel)
    except Exception as e:  # noqa: BLE001
        log.exception("advisor_send_failed", chat_id=chat_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"Fallo enviando: {e}") from e

    # Persistencia: marca como sender=advisor para que el dashboard lo distinga
    await memory.append(
        chat_id,
        "assistant",
        text,
        metadata={"sender": "advisor", "advisor_name": body.advisor_name or "Recepción"},
    )
    return {"ok": True}


@router.post("/conversations/{chat_id}/read", dependencies=[ApiAuth])
async def mark_read(chat_id: str) -> dict[str, Any]:
    await bot_settings.mark_read(chat_id)
    return {"ok": True}


@router.get("/options", dependencies=[ApiAuth])
async def list_options() -> dict[str, Any]:
    """Devuelve las opciones de selects desde crm_atributo_opciones agrupadas por campo."""
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("crm_atributo_opciones")
            .select("campo, valor, etiqueta, color, orden")
            .order("campo")
            .order("orden")
            .execute()
        )
    )
    grouped: dict[str, list] = {}
    for r in res.data or []:
        grouped.setdefault(r["campo"], []).append({
            "valor": r["valor"],
            "etiqueta": r["etiqueta"],
            "color": r.get("color"),
        })
    return grouped
