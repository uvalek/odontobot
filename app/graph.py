"""Grafo principal con LangGraph.

Nodos:
    load_buffer → resolve_media → load_memory → router →
        agent_dispatch (M1|M2|M3|M4) → split → send → save_memory
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

import structlog
from langgraph.graph import END, StateGraph

from app.agents import extractor, m1_faq, m2_agendamiento, m3_catalogo, m4_seguimiento, router
from app.channels import manychat as manychat_chan
from app.channels import telegram as telegram_chan
from app import bot_settings, clinic_profile, memory
from app.config import get_settings
from app.media import describe_image, transcribe_audio
from app.security import input_guard, output_guard, security_log, token_budget
from app.security.prompt_fence import wrap_user_message
from app.splitter import split_response
from app.tools import contactos
from app.tools.cal import _normalize_phone
from app.tools.contactos import merge_lead_fields

log = structlog.get_logger(__name__)

# Respuesta amable si un agente truena (error de OpenAI, red, modelo, etc.).
# Evita que el bot se quede MUDO: el usuario siempre recibe algo y el error
# real queda en los logs para diagnóstico.
_AGENT_FALLBACK = clinic_profile.TECH_FALLBACK_TEXT

# Marcador que los agentes agregan a su respuesta cuando el caso debe pasar a
# un especialista (GATE 3). `_split` lo quita antes de enviar y `_save_memory`
# apaga el bot para esa conversación con el mismo toggle del dashboard.
HANDOFF_MARKER = "[[HANDOFF]]"


class ChatState(TypedDict, total=False):
    chat_id: str
    channel: Literal["telegram", "manychat", "webchat"]
    subchannel: Literal["whatsapp", "instagram", "messenger", "telegram", "webchat"]
    raw_messages: list[dict[str, Any]]
    user_text: str          # texto FENCED, va al LLM (seguro)
    user_text_raw: str      # texto sin fence (memoria, extractor)
    user_phone: str
    history: list[dict[str, str]]
    route: Literal["M1", "M2", "M3", "M4"]
    agent_response: str
    chunks: list[str]
    handoff: bool           # el agente pidió pasar la conversación a un especialista
    blocked: bool           # input_guard decidió BLOCK
    suspicious: bool        # input_guard decidió SUSPICIOUS (logueado)


async def _resolve_media(state: ChatState) -> dict[str, Any]:
    pieces: list[str] = []
    for row in state["raw_messages"]:
        if row.get("text"):
            pieces.append(row["text"])
            continue
        media_type = row.get("media_type")
        url = row.get("media_url")
        if not (media_type and url):
            continue
        try:
            if media_type == "audio":
                pieces.append(await transcribe_audio(url))
            elif media_type == "image":
                desc = await describe_image(url)
                pieces.append(f"[Imagen recibida] {desc}")
        except Exception as e:  # noqa: BLE001
            log.warning("media_resolve_failed", media_type=media_type, error=str(e))
    raw = "\n".join(p for p in pieces if p).strip()

    # --- Capa 3: input guard ---
    guard = input_guard.classify(raw)
    chat_id = state.get("chat_id", "")
    channel = state.get("channel", "")

    settings = get_settings()
    # --- Circuit breaker global ---
    if token_budget.global_over_budget(settings.global_token_budget_per_day):
        security_log.log_event(
            "global_budget_exhausted",
            chat_id=chat_id,
            severity="critical",
            channel=channel,
        )
        return {
            "user_text": "",
            "user_text_raw": raw,
            "blocked": True,
            "agent_response": "Servicio temporalmente no disponible. Intenta más tarde.",
        }

    # --- Presupuesto por chat ---
    if token_budget.chat_over_budget(chat_id, settings.chat_token_budget_per_day):
        security_log.log_event(
            "chat_budget_exhausted",
            chat_id=chat_id,
            severity="warning",
            channel=channel,
            tokens_used=token_budget.chat_usage(chat_id),
        )
        return {
            "user_text": "",
            "user_text_raw": raw,
            "blocked": True,
            "agent_response": clinic_profile.BUDGET_EXHAUSTED_TEXT,
        }

    if guard.is_block:
        security_log.log_event(
            "input_blocked",
            chat_id=chat_id,
            severity="warning",
            channel=channel,
            reasons=",".join(guard.reasons),
            invisible=guard.invisible_chars_stripped,
        )
        return {
            "user_text": "",
            "user_text_raw": raw,
            "blocked": True,
            "agent_response": input_guard.BLOCK_RESPONSE_TEXTS[0],
        }

    # Texto saneado (sin caracteres invisibles). Para memoria/extractor.
    sanitized = guard.sanitized or raw
    if guard.is_suspicious:
        security_log.log_event(
            "input_suspicious",
            chat_id=chat_id,
            severity="info",
            channel=channel,
            reasons=",".join(guard.reasons),
        )

    # --- Capa 2: fence el texto antes de mandarlo al modelo ---
    fenced = wrap_user_message(sanitized)
    return {
        "user_text": fenced,
        "user_text_raw": sanitized,
        "suspicious": guard.is_suspicious,
    }


def _blocked_branch(state: ChatState) -> str:
    return "split" if state.get("blocked") else "load_memory"


async def _load_memory(state: ChatState) -> dict[str, Any]:
    history = await memory.load_history(state["chat_id"])
    return {"history": history}


async def _route(state: ChatState) -> dict[str, Any]:
    try:
        code = await router.classify(state.get("user_text", ""), state.get("history"))
    except Exception as e:  # noqa: BLE001
        log.exception("router_failed", error=str(e))
        code = "M3"  # ante la duda, servicios (el mismo default del router)
    return {"route": code}


def _route_branch(state: ChatState) -> str:
    return state.get("route") or "M3"


async def _m1(state: ChatState) -> dict[str, Any]:
    try:
        text = await m1_faq.respond(state["user_text"], state.get("history", []))
    except Exception as e:  # noqa: BLE001
        log.exception("agent_failed", agent="M1", error=str(e))
        text = _AGENT_FALLBACK
    return {"agent_response": text}


async def _m2(state: ChatState) -> dict[str, Any]:
    # Sub-canal visible (whatsapp/instagram/messenger/telegram) — preferimos
    # el explicito si vino del webhook; si no, derivamos del canal interno.
    canal_visible = state.get("subchannel") or (
        "whatsapp" if state.get("channel") == "manychat" else state.get("channel", "")
    )
    try:
        text = await m2_agendamiento.respond(
            state["user_text"],
            state.get("history", []),
            user_phone=state.get("user_phone", ""),
            chat_id=state.get("chat_id", ""),
            canal=canal_visible,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("agent_failed", agent="M2", error=str(e))
        text = _AGENT_FALLBACK
    return {"agent_response": text}


async def _m3(state: ChatState) -> dict[str, Any]:
    try:
        text = await m3_catalogo.respond(state["user_text"], state.get("history", []))
    except Exception as e:  # noqa: BLE001
        log.exception("agent_failed", agent="M3", error=str(e))
        text = _AGENT_FALLBACK
    return {"agent_response": text}


async def _m4(state: ChatState) -> dict[str, Any]:
    try:
        text = await m4_seguimiento.respond(state["user_text"], state.get("history", []))
    except Exception as e:  # noqa: BLE001
        log.exception("agent_failed", agent="M4", error=str(e))
        text = _AGENT_FALLBACK
    return {"agent_response": text}


async def _split(state: ChatState) -> dict[str, Any]:
    response = state.get("agent_response") or ""
    # GATE 3: el agente marcó la conversación para un especialista. Quitamos el
    # marcador para que nunca llegue al paciente.
    handoff = HANDOFF_MARKER in response
    if handoff:
        response = response.replace(HANDOFF_MARKER, "")
    chunks = split_response(response)
    # Capa 4: output guard antes de salir.
    chunks, reasons = output_guard.sanitize_chunks(chunks)
    if reasons:
        security_log.log_event(
            "output_sanitized",
            chat_id=state.get("chat_id", ""),
            severity="warning",
            channel=state.get("channel", ""),
            reasons=",".join(reasons[:5]),
        )
    return {"chunks": chunks, "agent_response": response, "handoff": handoff}


async def _send(state: ChatState) -> dict[str, Any]:
    chunks = state.get("chunks") or []
    if state["channel"] == "telegram":
        await telegram_chan.send_messages(state["chat_id"], chunks)
    elif state["channel"] == "manychat":
        sub = state.get("subchannel") or "whatsapp"
        await manychat_chan.send_messages(state["chat_id"], chunks, subchannel=sub)
    # webchat: no hay envio saliente; la respuesta vuelve en el HTTP response
    # sincronico (ver dispatch_webchat).
    return {}


async def _save_memory(state: ChatState) -> dict[str, Any]:
    # Memoria guarda el texto crudo (no el envuelto en fence) para que el
    # historial siga siendo legible para el modelo en turnos posteriores.
    raw = state.get("user_text_raw") or ""
    if raw:
        await memory.append(state["chat_id"], "user", raw)
    # Guarda cada burbuja por separado (igual que se envían al usuario) para
    # que el historial y el dashboard muestren mensajes individuales, no el
    # array JSON crudo en un solo registro. Fallback a agent_response si por
    # algún motivo no hubo chunks.
    chunks = state.get("chunks") or []
    if chunks:
        for chunk in chunks:
            await memory.append(state["chat_id"], "assistant", chunk)
    elif state.get("agent_response"):
        await memory.append(state["chat_id"], "assistant", state["agent_response"])
    # Estimación rápida de tokens consumidos (input + output) para el budget.
    used = token_budget.estimate_tokens(raw) + token_budget.estimate_tokens(
        state.get("agent_response") or ""
    )
    if used:
        token_budget.record(state.get("chat_id", ""), used)
    if state.get("handoff"):
        await _apply_handoff(state)
    return {}


async def _apply_handoff(state: ChatState) -> None:
    """Apaga el bot para esta conversación (mismo toggle que usa el dashboard)
    y marca la etapa `handoff` en el CRM. Corre después de enviar el aviso."""
    chat_id = state.get("chat_id", "")
    channel = state.get("channel", "")
    try:
        await bot_settings.set_enabled(chat_id, False, channel=channel)
    except Exception as e:  # noqa: BLE001
        log.exception("handoff_toggle_failed", chat_id=chat_id, error=str(e))
    try:
        canal_visible = state.get("subchannel") or channel
        await contactos.mark_handoff(chat_id, canal=canal_visible)
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_mark_failed", chat_id=chat_id, error=str(e))
    log.info("handoff_applied", chat_id=chat_id, channel=channel)


async def _extract_lead(state: ChatState) -> dict[str, Any]:
    """Corre despues de send: extrae datos del lead y mergea en `contactos`
    sin sobrescribir lo que el personal haya editado a mano."""
    if state.get("blocked"):
        return {}
    text = state.get("user_text_raw") or ""
    if not text.strip():
        return {}
    try:
        fields = await extractor.extract(text, state.get("history", []))
        if not fields:
            return {}
        canal_visible = state.get("subchannel") or (
            "whatsapp" if state.get("channel") == "manychat" else state.get("channel", "")
        )
        await merge_lead_fields(
            chat_id=state.get("chat_id", ""),
            canal=canal_visible,
            fields=fields,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("extract_lead_failed", error=str(e))
    return {}


def build_graph():
    g: StateGraph = StateGraph(ChatState)
    g.add_node("resolve_media", _resolve_media)
    g.add_node("load_memory", _load_memory)
    g.add_node("router", _route)
    g.add_node("M1", _m1)
    g.add_node("M2", _m2)
    g.add_node("M3", _m3)
    g.add_node("M4", _m4)
    g.add_node("split", _split)
    g.add_node("send", _send)
    g.add_node("save_memory", _save_memory)
    g.add_node("extract_lead", _extract_lead)

    g.set_entry_point("resolve_media")
    # Si input_guard / budget bloquearon, saltamos memoria + router + agentes
    # y vamos directo a split/send con la respuesta neutral ya seteada.
    g.add_conditional_edges(
        "resolve_media",
        _blocked_branch,
        {"split": "split", "load_memory": "load_memory"},
    )
    g.add_edge("load_memory", "router")
    g.add_conditional_edges(
        "router",
        _route_branch,
        {"M1": "M1", "M2": "M2", "M3": "M3", "M4": "M4"},
    )
    for n in ("M1", "M2", "M3", "M4"):
        g.add_edge(n, "split")
    g.add_edge("split", "send")
    g.add_edge("send", "save_memory")
    g.add_edge("save_memory", "extract_lead")
    g.add_edge("extract_lead", END)
    return g.compile()


_GRAPH = None


def graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


async def dispatch(chat_id: str, channel: str, messages: list[dict[str, Any]]) -> None:
    """Punto de entrada que usa el buffer cuando expira la ventana."""
    # Detecta sub-canal visible (whatsapp/instagram/messenger). Lo inyectamos
    # en el payload al recibir el webhook (`__subchannel`).
    subchannel = "telegram" if channel == "telegram" else "whatsapp"
    for row in messages:
        payload = row.get("payload") or {}
        if isinstance(payload, dict):
            sub = payload.get("__subchannel")
            if sub in ("whatsapp", "instagram", "messenger", "telegram"):
                subchannel = sub
                break

    user_phone = ""
    for row in messages:
        payload = row.get("payload") or {}
        if channel == "telegram":
            ph = payload.get("contact", {}).get("phone_number") if isinstance(payload, dict) else None
        else:
            ph = (payload.get("phone") or payload.get("whatsapp_phone")) if isinstance(payload, dict) else None
        if ph:
            user_phone = ph
            break

    # Sanea valores basura tipo "{{phone}}" (placeholder no resuelto de ManyChat)
    # o numeros mal formateados. Si no es E.164 valido -> "" y no se guarda nada.
    user_phone = _normalize_phone(user_phone) or ""

    # Solo en WhatsApp tiene sentido pedir el telefono via API de ManyChat.
    # Instagram y Messenger NO tienen telefono asociado al subscriber, asi que
    # ese flujo lo cubre el agente preguntandolo al usuario.
    if not user_phone and channel == "manychat" and subchannel == "whatsapp":
        api_phone = await manychat_chan.fetch_subscriber_phone(chat_id)
        user_phone = _normalize_phone(api_phone) or ""

    state: ChatState = {
        "chat_id": chat_id,
        "channel": channel,  # type: ignore[typeddict-item]
        "subchannel": subchannel,  # type: ignore[typeddict-item]
        "raw_messages": messages,
        "user_phone": user_phone,
    }
    await graph().ainvoke(state)


async def dispatch_webchat(chat_id: str, text: str) -> list[str]:
    """Punto de entrada SINCRONO para el widget de chat web.

    A diferencia de Telegram/ManyChat (asincronos: el webhook responde y el
    bot envia luego), aqui el usuario espera la respuesta en el mismo request.
    Corremos el grafo y devolvemos los `chunks` directamente; el nodo `send`
    es un no-op para el canal `webchat`.
    """
    state: ChatState = {
        "chat_id": chat_id,
        "channel": "webchat",  # type: ignore[typeddict-item]
        "subchannel": "webchat",  # type: ignore[typeddict-item]
        "raw_messages": [{"text": text}],
        "user_phone": "",
    }
    result = await graph().ainvoke(state)
    return result.get("chunks") or []
