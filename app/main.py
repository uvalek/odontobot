"""FastAPI: webhooks de Telegram y ManyChat."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import deque
from typing import Any

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app import api as dashboard_api
from app import bot_settings, buffer, channel_flags, memory, rate_limit
from app.channels import manychat as manychat_chan
from app.channels import telegram as telegram_chan
from app.config import get_settings
from app.graph import dispatch, dispatch_webchat
from app.security import input_guard, security_log
from pydantic import BaseModel

settings = get_settings()
logging.basicConfig(level=settings.log_level)
# Silencia los INFO de httpx/httpcore (cada llamada a Supabase/OpenAI/ManyChat
# generaba 1-2 lineas que llenaban los logs sin aportar diagnostico). Solo
# warnings y errores de esas libs aparecen.
for noisy in ("httpx", "httpcore", "openai._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = structlog.get_logger(__name__)

app = FastAPI(title="Odontobot demo — Clínica Dental")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defensive HTTP headers to every response.

    No CSP here on purpose: the /panel page uses inline scripts and the
    public widget always reaches us cross-origin from the Next.js proxy,
    so a strict CSP would either need a nonce-aware rewrite or block
    legitimate traffic. The headers below are framework-agnostic and
    safe to apply blanket-style.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rechaza requests con Content-Length excesivo antes de leerlos.

    Capa anti-DoS contra payloads gigantes. El límite real de WhatsApp /
    Telegram es muy bajo (<100 KB típico) así que 1 MB sobra para uso
    normal y bloquea abusos. Configurable via MAX_REQUEST_BODY_BYTES.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        max_bytes = get_settings().max_request_body_bytes
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            return Response(status_code=413, content="payload too large")
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


# Dedup de update_id de Telegram (idempotencia). Ventana corta en memoria:
# si el contenedor reinicia podríamos reprocesar un mensaje, pero el
# coste es bajo y la implementación tiene complejidad cero.
_RECENT_TG_IDS: deque[int] = deque(maxlen=2000)
_RECENT_TG_SET: set[int] = set()


def _telegram_dedupe(update_id: int | None) -> bool:
    """True si ya vimos ese update_id en los últimos N. Idempotencia."""
    if update_id is None:
        return False
    if update_id in _RECENT_TG_SET:
        return True
    _RECENT_TG_IDS.append(update_id)
    _RECENT_TG_SET.add(update_id)
    if len(_RECENT_TG_IDS) >= _RECENT_TG_IDS.maxlen:
        # mantiene el set sincronizado al rotar la deque
        _RECENT_TG_SET.clear()
        _RECENT_TG_SET.update(_RECENT_TG_IDS)
    return False


def _check_chat_rate_limit(chat_id: str, channel: str) -> tuple[bool, int]:
    """Aplica límite por minuto y por día al chat_id. Devuelve (allowed, retry_after)."""
    s = get_settings()
    ok_min, retry_min = rate_limit.hit(
        f"chat:{chat_id}:min", s.chat_rate_limit_per_min, 60
    )
    if not ok_min:
        security_log.log_event(
            "chat_rate_limit_minute",
            chat_id=chat_id,
            severity="warning",
            channel=channel,
            retry_after=retry_min,
        )
        return False, retry_min
    ok_day, retry_day = rate_limit.hit(
        f"chat:{chat_id}:day", s.chat_rate_limit_per_day, 86400
    )
    if not ok_day:
        security_log.log_event(
            "chat_rate_limit_day",
            chat_id=chat_id,
            severity="warning",
            channel=channel,
        )
        return False, retry_day
    return True, 0

# CORS: permite al dashboard (Vercel + localhost dev) consumir /api/*.
# El regex por defecto vacio: si tu deploy lo necesita, sobreescribe via
# DASHBOARD_CORS_ORIGIN_REGEX (preferiblemente un regex pegado a TU
# dominio, no a *.vercel.app).
_origins = [o.strip() for o in settings.dashboard_cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=settings.dashboard_cors_origin_regex or None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(dashboard_api.router)


# ---------------------------------------------------------------------------
# Webchat shared-secret guard. The endpoint is meant to be reached only by
# the Next.js proxy (which sends `X-API-Key`). If `WEBCHAT_API_KEY` is not
# configured we fall back to the previous open behavior so existing
# deployments are not broken by this rollout.
# ---------------------------------------------------------------------------


def _require_webchat_key(x_api_key: str | None) -> None:
    expected = settings.webchat_api_key
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="webchat api key invalido")

# Conjunto de tasks vivos. Sin esta referencia fuerte el GC puede matar
# `asyncio.create_task(...)` a media ejecucion (gotcha conocido de Python).
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


async def _reaper_loop() -> None:
    """Procesa mensajes huérfanos del buffer (cuando el contenedor reinicia
    mientras un `schedule_flush` dormía sus 25 s). Corre dentro del mismo
    proceso web para no depender de un segundo servicio en EasyPanel.
    """
    interval = max(10, settings.reaper_interval_seconds)
    log.info("reaper_start", interval=interval)
    tick = 0
    while True:
        try:
            n = await buffer.reap_orphans(dispatch)
            if n:
                log.info("reaper_processed", count=n)
        except Exception as e:  # noqa: BLE001
            log.exception("reaper_error", error=str(e))
        tick += 1
        if tick % 30 == 0:  # heartbeat cada ~30 min
            log.info("reaper_heartbeat", tick=tick)
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _startup() -> None:
    # Pasada inmediata para limpiar lo que quedó en vuelo del contenedor anterior
    try:
        n = await buffer.reap_orphans(dispatch)
        if n:
            log.info("reaper_boot_processed", count=n)
    except Exception as e:  # noqa: BLE001
        log.exception("reaper_boot_error", error=str(e))
    _spawn(_reaper_loop())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Version "marker" hardcoded — se actualiza con cada feature releveante para
# poder verificar que EasyPanel redeployo. Subir el numero a mano en cada
# cambio que necesite confirmacion en produccion.
_VERSION = "demo-dental-v1-2026-09-13"


@app.get("/version")
async def version() -> dict[str, str]:
    return {"version": _VERSION}


@app.get("/admin/reap")
async def admin_reap(token: str | None = None) -> dict[str, object]:
    """Fuerza una pasada del reaper. Util para diagnostico cuando un mensaje
    queda atascado en buffer."""
    _check_token(token)
    n = await buffer.reap_orphans(dispatch)
    return {"processed": n, "bg_tasks": len(_BG_TASKS)}


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    # Comparación constant-time del secret (antes usaba !=, vulnerable a
    # timing attacks). Si la variable no está configurada, no exigimos
    # header (compatibilidad), pero registramos un warning una sola vez.
    if settings.telegram_webhook_secret:
        provided = x_telegram_bot_api_secret_token or ""
        expected = settings.telegram_webhook_secret
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            security_log.log_event(
                "webhook_invalid_signature",
                chat_id=None,
                severity="warning",
                channel="telegram",
            )
            raise HTTPException(status_code=403, detail="invalid secret")

    update = await request.json()

    # Idempotencia: Telegram reintenta si tardamos en responder.
    if _telegram_dedupe(update.get("update_id") if isinstance(update, dict) else None):
        return {"status": "duplicate"}

    parsed = telegram_chan.parse_update(update)
    if not parsed:
        return {"status": "ignored"}

    # Interruptor global del canal Telegram (panel /panel).
    if not await channel_flags.is_enabled("telegram"):
        log.info("telegram_channel_off", chat_id=parsed["chat_id"])
        return {"status": "channel_off"}

    # Rate limit por chat: detiene un usuario que mande N mensajes/min.
    ok, retry = _check_chat_rate_limit(parsed["chat_id"], "telegram")
    if not ok:
        return {"status": "rate_limited", "retry_after": str(retry)}

    media_url = None
    if parsed["media_file_id"]:
        try:
            media_url = await telegram_chan.resolve_file_url(parsed["media_file_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("telegram_file_resolve_failed", error=str(e))

    # Toggle per-conversacion del dashboard: si el personal apago el bot para
    # este chat, guardamos el mensaje en historial (para que aparezca en la UI)
    # pero no lo metemos al buffer ni disparamos al bot.
    if not await bot_settings.is_enabled(parsed["chat_id"]):
        await _store_user_message(parsed["chat_id"], parsed["text"], parsed["media_type"])
        return {"status": "queued_no_bot"}

    # Persiste canal + handle (telefono/@user/nombre) al primer mensaje.
    try:
        await bot_settings.ensure_row(parsed["chat_id"], "telegram")
        await _ensure_canal(parsed["chat_id"], "telegram", handle=parsed.get("handle"))
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_persist_canal_failed", error=str(e), chat_id=parsed["chat_id"])

    await buffer.insert_message(
        chat_id=parsed["chat_id"],
        channel="telegram",
        payload=parsed["raw"],
        text=parsed["text"],
        media_type=parsed["media_type"],
        media_url=media_url,
    )
    _spawn(buffer.schedule_flush(parsed["chat_id"], "telegram", dispatch))
    return {"status": "queued"}


@app.api_route("/webhook/manychat", methods=["GET", "POST"])
async def manychat_webhook(
    request: Request,
    x_manychat_secret: str | None = Header(default=None),
) -> dict[str, str]:
    # Shared secret opt-in. Si configuras MANYCHAT_WEBHOOK_SECRET en
    # EasyPanel y agregas un header `X-ManyChat-Secret` con el mismo valor
    # al External Request de ManyChat, nadie más puede mandarle POSTs
    # falsos al endpoint.
    expected = settings.manychat_webhook_secret
    if expected:
        provided = x_manychat_secret or ""
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            security_log.log_event(
                "webhook_invalid_signature",
                chat_id=None,
                severity="warning",
                channel="manychat",
            )
            raise HTTPException(status_code=403, detail="invalid secret")

    if request.method == "GET":
        # ManyChat (plan básico) solo permite GET con query params
        body = dict(request.query_params)
        # Reconstruye estructura `last_interaction` si vienen mime/url
        if body.get("media_url") or body.get("mime_type"):
            body["last_interaction"] = {
                "url": body.get("media_url", ""),
                "mime_type": body.get("mime_type", ""),
            }
    else:
        try:
            body = await request.json()
        except Exception as e:  # noqa: BLE001
            log.warning("manychat_body_parse_failed", error=str(e))
            body = dict(request.query_params)

    log.info(
        "manychat_webhook_in",
        method=request.method,
        body_keys=list(body.keys()) if isinstance(body, dict) else None,
        body_preview={k: (str(v)[:80] if not isinstance(v, dict) else "<dict>") for k, v in (body.items() if isinstance(body, dict) else [])},
    )

    parsed = manychat_chan.parse_webhook(body)
    if not parsed:
        log.warning("manychat_parse_returned_none", body=body)
        return {"status": "ignored"}

    log.info(
        "manychat_parsed",
        chat_id=parsed["chat_id"],
        subchannel=parsed.get("subchannel"),
        has_text=bool(parsed.get("text")),
        media_type=parsed.get("media_type"),
    )

    # Interruptor global del sub-canal (whatsapp/instagram/messenger) — panel /panel.
    subchannel = parsed.get("subchannel") or "whatsapp"
    if not await channel_flags.is_enabled(subchannel):
        log.info("manychat_channel_off", chat_id=parsed["chat_id"], subchannel=subchannel)
        return {"status": "channel_off"}

    # Rate limit por chat.
    ok, retry = _check_chat_rate_limit(parsed["chat_id"], subchannel)
    if not ok:
        return {"status": "rate_limited", "retry_after": str(retry)}

    # Toggle per-conversacion (ver telegram_webhook).
    if not await bot_settings.is_enabled(parsed["chat_id"]):
        log.info("manychat_queued_no_bot", chat_id=parsed["chat_id"])
        await _store_user_message(parsed["chat_id"], parsed["text"], parsed["media_type"])
        return {"status": "queued_no_bot"}

    # Persiste el sub-canal visible (whatsapp/instagram/messenger) en
    # bot_settings y contactos.canal para que el dashboard lo etiquete bien.
    # Tambien intenta resolver un "handle" identificable (telefono / nombre /
    # @ig_username) llamando a ManyChat getInfo. Best-effort: si falla, no
    # bloquea el flujo del bot.
    handle: str | None = None
    # 1) Body del webhook (lo mas confiable cuando el flow incluye el campo).
    handle = manychat_chan.derive_handle_from_payload(subchannel, body)
    # 2) Si no, fetch a la API de ManyChat (best-effort).
    if not handle:
        try:
            info = await manychat_chan.fetch_subscriber_info(parsed["chat_id"])
            if info:
                handle = manychat_chan.derive_handle(subchannel, info)
                # Log diagnostico para ver que devuelve ManyChat realmente
                log.info(
                    "manychat_subscriber_info_ok",
                    chat_id=parsed["chat_id"],
                    fields_present=[
                        k for k in ("phone", "whatsapp_phone", "name", "ig_username", "first_name")
                        if info.get(k)
                    ],
                    handle_resolved=handle,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("manychat_fetch_info_failed", error=str(e), chat_id=parsed["chat_id"])
    try:
        await bot_settings.ensure_row(parsed["chat_id"], "manychat")
        await _ensure_canal(parsed["chat_id"], subchannel, handle=handle)
    except Exception as e:  # noqa: BLE001
        log.warning("manychat_persist_canal_failed", error=str(e), chat_id=parsed["chat_id"])

    # Inyectamos el subchannel en el payload del buffer para que el dispatch lo
    # recupere despues de la ventana (no podemos pasarlo por la firma, esa la
    # comparte con telegram).
    payload_with_sub = dict(parsed["raw"]) if isinstance(parsed["raw"], dict) else {}
    payload_with_sub["__subchannel"] = subchannel

    await buffer.insert_message(
        chat_id=parsed["chat_id"],
        channel="manychat",
        payload=payload_with_sub,
        text=parsed["text"],
        media_type=parsed["media_type"],
        media_url=parsed["media_url"],
    )
    _spawn(buffer.schedule_flush(parsed["chat_id"], "manychat", dispatch))
    return {"status": "queued"}


async def _ensure_canal(chat_id: str, canal: str, handle: str | None = None) -> None:
    """Asegura que la fila de `contactos` exista para esta conversacion.

    - `canal`: el webhook es la fuente de verdad (no es editable desde el CRM),
      asi que siempre se pisa con el ultimo valor recibido.
    - `handle`: identificador legible auto-detectado por canal (telefono WA,
      @ TG/IG, nombre FB). Solo se actualiza si la fila no lo tiene; el
      usuario podria haber dado luego un dato mejor (su nombre real va a `nombre`
      via el extractor M0).
    - NO toca `nombre` (lo gestiona el extractor o el personal manualmente).
    """
    from app.db import supabase  # import local
    existing = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .select("id, canal, handle")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
    )
    rows = existing.data or []
    if not rows:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "canal": canal,
            "etapa_seguimiento": "nuevo",
        }
        if handle:
            payload["handle"] = handle
        # nombre arranca como NULL — el dashboard caera en handle como fallback.
        await asyncio.to_thread(
            lambda: supabase().table("contactos").insert(payload).execute()
        )
        return
    row = rows[0]
    update: dict[str, Any] = {}
    if row.get("canal") != canal:
        update["canal"] = canal
    if handle and not row.get("handle"):
        update["handle"] = handle
    if update:
        await asyncio.to_thread(
            lambda: (
                supabase()
                .table("contactos")
                .update(update)
                .eq("id", row["id"])
                .execute()
            )
        )


async def _store_user_message(chat_id: str, text: str | None, media_type: str | None) -> None:
    """Guarda un mensaje entrante en historial sin disparar al bot.
    Usado cuando el bot esta apagado para esa conversacion (manual o handoff)."""
    content = text or ""
    if not content:
        if media_type == "audio":
            content = "[Audio recibido]"
        elif media_type == "image":
            content = "[Imagen recibida]"
        else:
            return
    try:
        await memory.append(chat_id, "user", content)
    except Exception as e:  # noqa: BLE001
        log.exception("store_user_msg_failed", chat_id=chat_id, error=str(e))


def _check_token(token: str | None) -> None:
    expected = settings.test_arm_token
    if not expected:
        raise HTTPException(status_code=503, detail="TEST_ARM_TOKEN no configurado")
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid token")


# ---------------------------------------------------------------------------
# Interruptores de canal. El panel /panel los maneja con el mismo token.
# ---------------------------------------------------------------------------


@app.get("/admin/channels")
async def admin_channels_get(token: str | None = None) -> dict[str, Any]:
    _check_token(token)
    return {"flags": await channel_flags.all_flags(force=True)}


@app.api_route("/admin/channels", methods=["POST"])
async def admin_channels_set(
    token: str | None = None,
    channel: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    _check_token(token)
    if channel not in channel_flags.CHANNELS:
        raise HTTPException(status_code=400, detail=f"canal inválido: {channel}")
    if enabled is None:
        raise HTTPException(status_code=400, detail="falta el parámetro enabled")
    flags = await channel_flags.set_enabled(channel, enabled)
    return {"flags": flags}


# Panel HTML autosuficiente: un solo archivo, sin build, sin frontend separado.
# El token se guarda en localStorage del navegador; el server NUNCA lo loguea.
_PANEL_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Panel del Bot — Canales</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f172a; color: #e2e8f0; padding: 24px;
  }
  .card {
    background: #1e293b; border-radius: 16px; padding: 32px;
    width: 100%; max-width: 440px;
    box-shadow: 0 20px 60px rgba(0,0,0,.4);
  }
  h1 { margin: 0 0 4px; font-size: 22px; }
  .sub { color: #94a3b8; font-size: 13px; margin-bottom: 22px; }
  .ch {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 16px; border-radius: 12px; margin-bottom: 10px;
    background: #0f172a; border: 1px solid #334155;
  }
  .ch .ico { font-size: 20px; width: 26px; text-align: center; }
  .ch .name { flex: 1; }
  .ch .name b { font-size: 15px; }
  .ch .name .st { display: block; font-size: 11px; color: #94a3b8; margin-top: 2px; }
  /* switch */
  .sw {
    position: relative; width: 52px; height: 30px; flex: 0 0 auto;
    border-radius: 999px; background: #475569; cursor: pointer;
    transition: background .18s; border: 0; padding: 0;
  }
  .sw.on { background: #22c55e; }
  .sw:disabled { opacity: .45; cursor: not-allowed; }
  .sw .knob {
    position: absolute; top: 3px; left: 3px; width: 24px; height: 24px;
    border-radius: 50%; background: #fff; transition: transform .18s;
  }
  .sw.on .knob { transform: translateX(22px); }
  label { display: block; font-size: 12px; color: #94a3b8; margin: 18px 0 6px; }
  input {
    width: 100%; padding: 10px 12px; border-radius: 8px;
    border: 1px solid #334155; background: #0f172a; color: #e2e8f0;
    font-size: 14px;
  }
  .msg { font-size: 12px; margin-top: 14px; min-height: 16px; }
  .msg.err { color: #f87171; }
  .msg.ok { color: #4ade80; }
  .footer { text-align: center; font-size: 11px; color: #64748b; margin-top: 16px; }
</style>
</head>
<body>
  <div class="card">
    <h1>Canales del bot</h1>
    <div class="sub">Enciende o apaga el bot en cada canal. El cambio es inmediato.</div>

    <div id="channels"></div>

    <label for="token">Token</label>
    <input id="token" type="password" placeholder="TEST_ARM_TOKEN" autocomplete="off" />

    <div class="msg" id="msg"></div>
    <div class="footer">Se refresca cada 10 s</div>
  </div>

<script>
const $ = (id) => document.getElementById(id);
const tokenEl = $("token");
const msg = $("msg");
const channelsEl = $("channels");

const CHANNELS = [
  { id: "webchat",   ico: "\\uD83D\\uDCBB", label: "Sitio web" },
  { id: "telegram",  ico: "\\u2708\\uFE0F", label: "Telegram" },
  { id: "whatsapp",  ico: "\\uD83D\\uDCAC", label: "WhatsApp" },
  { id: "instagram", ico: "\\uD83D\\uDCF7", label: "Instagram" },
  { id: "messenger", ico: "\\uD83D\\uDCE8", label: "Messenger" },
];

tokenEl.value = localStorage.getItem("bot_token") || "";
tokenEl.addEventListener("input", () => localStorage.setItem("bot_token", tokenEl.value));

let flags = {};
let busy = false;

function render() {
  const hasToken = !!tokenEl.value.trim();
  channelsEl.innerHTML = "";
  for (const c of CHANNELS) {
    const on = !!flags[c.id];
    const row = document.createElement("div");
    row.className = "ch";
    row.innerHTML = `
      <div class="ico">${c.ico}</div>
      <div class="name">
        <b>${c.label}</b>
        <span class="st">${on ? "Encendido" : "Apagado"}</span>
      </div>
      <button class="sw ${on ? "on" : ""}" data-ch="${c.id}" ${hasToken && !busy ? "" : "disabled"}>
        <span class="knob"></span>
      </button>`;
    channelsEl.appendChild(row);
  }
  channelsEl.querySelectorAll(".sw").forEach((b) => {
    b.addEventListener("click", () => toggle(b.dataset.ch));
  });
}

function showMsg(text, isErr = false) {
  msg.textContent = text;
  msg.className = "msg " + (isErr ? "err" : "ok");
  setTimeout(() => { msg.textContent = ""; msg.className = "msg"; }, 4000);
}

async function refresh() {
  const t = tokenEl.value.trim();
  if (!t) { flags = {}; render(); return; }
  try {
    const r = await fetch(`/admin/channels?token=${encodeURIComponent(t)}`);
    if (!r.ok) { showMsg(`Error ${r.status}`, true); return; }
    const data = await r.json();
    flags = data.flags || {};
    render();
  } catch (e) { showMsg("Error de red: " + e.message, true); }
}

async function toggle(ch) {
  const t = tokenEl.value.trim();
  if (!t || busy) return;
  busy = true; render();
  const next = !flags[ch];
  const url = new URL("/admin/channels", window.location.origin);
  url.searchParams.set("token", t);
  url.searchParams.set("channel", ch);
  url.searchParams.set("enabled", next ? "true" : "false");
  try {
    const r = await fetch(url, { method: "POST" });
    if (!r.ok) {
      const txt = await r.text();
      showMsg(`Error ${r.status}: ${txt.slice(0, 120)}`, true);
    } else {
      const data = await r.json();
      flags = data.flags || flags;
      showMsg(`${ch}: ${next ? "encendido" : "apagado"}`, false);
    }
  } catch (e) {
    showMsg("Error de red: " + e.message, true);
  }
  busy = false; render();
}

tokenEl.addEventListener("change", refresh);
render();
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


@app.get("/panel", response_class=HTMLResponse)
async def panel() -> HTMLResponse:
    """Panel visual para encender/apagar el bot por canal. El token se guarda
    en localStorage del navegador; el server nunca lo loguea ni lo expone."""
    return HTMLResponse(_PANEL_HTML)


# ---------------------------------------------------------------------------
# Chat web (widget en la pagina). Canal SINCRONO: el navegador manda el texto
# y espera la respuesta en el mismo request, sin buffer ni envio saliente.
# ---------------------------------------------------------------------------


class WebchatIn(BaseModel):
    chat_id: str
    text: str


@app.post("/api/webchat")
async def webchat(
    payload: WebchatIn,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    # Shared-secret check (opt-in). If WEBCHAT_API_KEY is set in env,
    # only callers that present the matching header get through.
    _require_webchat_key(x_api_key)

    # Per-IP rate limit (sliding window, in-process). Reasonable defaults
    # are enforced by settings; setting webchat_rate_limit_per_min=0 disables.
    rl_limit = settings.webchat_rate_limit_per_min
    if rl_limit > 0:
        ok, retry_after = rate_limit.hit(
            f"webchat:{rate_limit.client_ip(request)}",
            limit=rl_limit,
            window_seconds=60,
        )
        if not ok:
            raise HTTPException(
                status_code=429,
                detail="rate_limited",
                headers={"Retry-After": str(retry_after)},
            )

    chat_id = (payload.chat_id or "").strip()
    text = (payload.text or "").strip()
    if not chat_id or not text:
        raise HTTPException(status_code=400, detail="chat_id y text son obligatorios")
    if len(text) > settings.webchat_max_text_len:
        # Antes: truncaba silenciosamente. Ahora: rechaza para que el
        # cliente sepa que paso. El proxy de Next.js ya limita a 4000.
        raise HTTPException(status_code=413, detail="text_too_long")
    # Interruptor global del canal web (panel /panel).
    if not await channel_flags.is_enabled("webchat"):
        log.info("webchat_channel_off", chat_id=chat_id)
        return {"chunks": ["El asistente no está disponible en este momento."]}
    # Rate limit por chat_id (además del per-IP). Un solo navegador puede
    # cambiar de IP (proxy, móvil) pero su chat_id en localStorage es estable.
    ok, retry = _check_chat_rate_limit(chat_id, "webchat")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="rate_limited",
            headers={"Retry-After": str(retry)},
        )
    # Persiste canal="webchat" en contactos para que el dashboard etiquete
    # bien la conversacion. Sin esto contactos.canal queda NULL y el dashboard
    # asume "whatsapp" por defecto (ver app/api.py). Tambien corrige las
    # conversaciones web que ya quedaron mal etiquetadas: _ensure_canal pisa
    # el canal con "webchat" si era distinto.
    try:
        await _ensure_canal(chat_id, "webchat")
    except Exception as e:  # noqa: BLE001
        log.warning("webchat_persist_canal_failed", error=str(e), chat_id=chat_id)

    # Toggle per-conversacion (ver telegram_webhook). Tambien lo apaga el
    # handoff automatico: el mensaje queda en historial para el especialista.
    if not await bot_settings.is_enabled(chat_id):
        log.info("webchat_queued_no_bot", chat_id=chat_id)
        await _store_user_message(chat_id, text, None)
        return {"chunks": []}

    log.info("webchat_in", chat_id=chat_id, text_len=len(text))
    try:
        chunks = await dispatch_webchat(chat_id, text)
    except Exception as e:  # noqa: BLE001
        log.exception("webchat_failed", chat_id=chat_id, error=str(e))
        raise HTTPException(status_code=500, detail="error procesando el mensaje")
    log.info("webchat_out", chat_id=chat_id, chunks=len(chunks))
    return {"chunks": chunks}
