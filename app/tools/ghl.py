"""Cliente de GoHighLevel (API v2, token de Private Integration).

Cubre lo que usa el bot:
  - Calendario: horarios libres, crear, listar, reagendar y cancelar citas.
  - Contactos: crear/actualizar paciente, etiquetas, notas y campos personalizados.

Todas las funciones son best-effort desde el punto de vista del bot: los
errores HTTP se levantan como `GHLError` con el cuerpo de la respuesta para
que el agente o el sync decidan qué hacer.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)

BASE = "https://services.leadconnectorhq.com"
V_CAL = "2021-04-15"
V_DEFAULT = "2021-07-28"
MX = ZoneInfo("America/Mexico_City")


class GHLError(RuntimeError):
    pass


def enabled() -> bool:
    s = get_settings()
    return bool(s.ghl_private_token and s.ghl_location_id)


def calendar_enabled() -> bool:
    return enabled() and bool(get_settings().ghl_calendar_id)


def _headers(version: str = V_DEFAULT) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().ghl_private_token}",
        "Version": version,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _request(
    method: str,
    path: str,
    *,
    version: str = V_DEFAULT,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(
            method, f"{BASE}{path}", headers=_headers(version), params=params, json=json
        )
    if r.status_code >= 400:
        body = r.text[:800]
        log.error("ghl_api_error", method=method, path=path.split("?")[0], status=r.status_code, body=body)
        raise GHLError(f"GHL {method} {path.split('?')[0]} {r.status_code}: {body}")
    if not r.content:
        return {}
    return r.json()


# ---------------------------------------------------------------------------
# Calendario
# ---------------------------------------------------------------------------

_calendar_cache: dict[str, Any] = {}


async def _calendar() -> dict[str, Any]:
    cid = get_settings().ghl_calendar_id
    cached = _calendar_cache.get(cid)
    if cached and time.time() - cached["at"] < 600:
        return cached["data"]
    data = (await _request("GET", f"/calendars/{cid}", version=V_CAL)).get("calendar") or {}
    _calendar_cache[cid] = {"at": time.time(), "data": data}
    return data


async def _slot_minutes() -> int:
    cal = await _calendar()
    duration = int(cal.get("slotDuration") or 30)
    unit = (cal.get("slotDurationUnit") or "mins").lower()
    return duration * 60 if unit.startswith("hour") else duration


def _to_utc_z(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _display(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MX)
    return dt.strftime("%I:%M %p").lstrip("0")


_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _fecha_mx(date_key: str) -> str:
    y, m, d = (int(x) for x in date_key.split("-"))
    dt = datetime(y, m, d)
    return f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]} de {dt.year}"


def normalize_free_slots(body: dict[str, Any]) -> dict[str, Any]:
    """Convierte la respuesta de free-slots al mismo formato que usaba Cal.com:
    {"availability_text", "slots": [{"start" (UTC Z), "display", "date"}]}."""
    days = {
        k: v.get("slots") or []
        for k, v in body.items()
        if isinstance(v, dict) and isinstance(v.get("slots"), list)
    }
    lines = ["Horarios disponibles:", ""]
    flat: list[dict[str, str]] = []
    for date_key in sorted(days):
        slots = days[date_key]
        if not slots:
            continue
        lines.append(f"{_fecha_mx(date_key)}:")
        for i, s in enumerate(slots, 1):
            display = _display(s)
            lines.append(f"{i}. {display}")
            flat.append({"start": _to_utc_z(s), "display": display, "date": date_key})
        lines.append("")
    if not flat:
        return {"availability_text": "No hay horarios disponibles.", "slots": []}
    lines.append("¿Cuál horario te acomoda?")
    return {"availability_text": "\n".join(lines), "slots": flat}


async def get_free_slots(start_iso: str, end_iso: str) -> dict[str, Any]:
    s = get_settings()
    body = await _request(
        "GET",
        f"/calendars/{s.ghl_calendar_id}/free-slots",
        version=V_CAL,
        params={
            "startDate": _ms(start_iso),
            "endDate": _ms(end_iso),
            "timezone": "America/Mexico_City",
        },
    )
    return normalize_free_slots(body)


async def create_appointment(*, contact_id: str, start_iso: str, title: str) -> dict[str, Any]:
    s = get_settings()
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = start + timedelta(minutes=await _slot_minutes())
    payload: dict[str, Any] = {
        "calendarId": s.ghl_calendar_id,
        "locationId": s.ghl_location_id,
        "contactId": contact_id,
        "startTime": start.astimezone(timezone.utc).isoformat(),
        "endTime": end.astimezone(timezone.utc).isoformat(),
        "title": title,
        "appointmentStatus": "confirmed",
        "toNotify": True,
    }
    return await _request("POST", "/calendars/events/appointments", version=V_CAL, json=payload)


async def contact_appointments(contact_id: str) -> list[dict[str, Any]]:
    """Citas futuras (no canceladas) del contacto en el calendario del bot."""
    body = await _request("GET", f"/contacts/{contact_id}/appointments")
    now = datetime.now(timezone.utc)
    cid = get_settings().ghl_calendar_id
    out = []
    for ev in body.get("events") or []:
        if cid and ev.get("calendarId") not in (None, cid):
            continue
        if (ev.get("appointmentStatus") or "").lower() in ("cancelled", "invalid"):
            continue
        # Este endpoint devuelve la hora local sin zona ("2026-09-15 12:30:00").
        try:
            start = datetime.fromisoformat(str(ev.get("startTime")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=MX)
        if start < now:
            continue
        out.append({**ev, "startTime": start.isoformat()})
    out.sort(key=lambda e: str(e.get("startTime")))
    return out


async def reschedule_appointment(event_id: str, new_start_iso: str) -> dict[str, Any]:
    start = datetime.fromisoformat(new_start_iso.replace("Z", "+00:00"))
    end = start + timedelta(minutes=await _slot_minutes())
    payload = {
        "calendarId": get_settings().ghl_calendar_id,
        "startTime": start.astimezone(timezone.utc).isoformat(),
        "endTime": end.astimezone(timezone.utc).isoformat(),
    }
    return await _request("PUT", f"/calendars/events/appointments/{event_id}", version=V_CAL, json=payload)


async def cancel_appointment(event_id: str) -> dict[str, Any]:
    payload = {"appointmentStatus": "cancelled"}
    return await _request("PUT", f"/calendars/events/appointments/{event_id}", version=V_CAL, json=payload)


# ---------------------------------------------------------------------------
# Contactos
# ---------------------------------------------------------------------------


def _split_name(nombre: str | None) -> tuple[str, str]:
    parts = (nombre or "").split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")


def _contact_payload(
    *,
    nombre: str | None = None,
    correo: str | None = None,
    telefono: str | None = None,
    custom_fields: list[dict[str, Any]] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if nombre:
        first, last = _split_name(nombre)
        payload.update({"name": nombre, "firstName": first, "lastName": last})
    if correo:
        payload["email"] = correo
    if telefono:
        payload["phone"] = telefono
    if custom_fields:
        payload["customFields"] = custom_fields
    if source:
        payload["source"] = source
    return payload


async def find_contact(*, correo: str | None = None, telefono: str | None = None) -> dict[str, Any] | None:
    params: dict[str, Any] = {"locationId": get_settings().ghl_location_id}
    if correo:
        params["email"] = correo
    elif telefono:
        params["number"] = telefono
    else:
        return None
    body = await _request("GET", "/contacts/search/duplicate", params=params)
    return body.get("contact") or None


async def upsert_contact(**kwargs: Any) -> dict[str, Any]:
    """Crea o actualiza por correo/teléfono (dedupe de GHL). Requiere uno de los dos."""
    payload = _contact_payload(**kwargs)
    payload["locationId"] = get_settings().ghl_location_id
    body = await _request("POST", "/contacts/upsert", json=payload)
    return body.get("contact") or {}


async def create_contact(**kwargs: Any) -> dict[str, Any]:
    """Crea un contacto sin dedupe (para visitantes sin correo ni teléfono)."""
    payload = _contact_payload(**kwargs)
    payload["locationId"] = get_settings().ghl_location_id
    body = await _request("POST", "/contacts/", json=payload)
    return body.get("contact") or {}


async def update_contact(contact_id: str, **kwargs: Any) -> dict[str, Any]:
    payload = _contact_payload(**kwargs)
    payload.pop("source", None)
    if not payload:
        return {}
    body = await _request("PUT", f"/contacts/{contact_id}", json=payload)
    return body.get("contact") or {}


async def add_tags(contact_id: str, tags: list[str]) -> None:
    if tags:
        await _request("POST", f"/contacts/{contact_id}/tags", json={"tags": tags})


async def add_note(contact_id: str, body: str) -> None:
    await _request("POST", f"/contacts/{contact_id}/notes", json={"body": body[:4000]})


# ---------------------------------------------------------------------------
# Campos personalizados (definición de dominio -> campo de GHL)
# ---------------------------------------------------------------------------

# Clave de dominio (la misma que usa el extractor) -> definición del campo en GHL.
# El bot busca cada campo por su nombre; `scripts/ghl_setup.py` los crea.
CUSTOM_FIELDS: dict[str, dict[str, Any]] = {
    "motivo_consulta": {"name": "Motivo de consulta", "dataType": "SINGLE_OPTIONS"},
    "nivel_urgencia": {"name": "Nivel de urgencia", "dataType": "SINGLE_OPTIONS",
                       "options": {"alta": "Alta", "media": "Media", "baja": "Baja"}},
    "tipo_paciente": {"name": "Tipo de paciente", "dataType": "SINGLE_OPTIONS",
                      "options": {"nuevo": "Nuevo", "seguimiento": "Seguimiento"}},
    "disponibilidad_preferida": {"name": "Disponibilidad preferida", "dataType": "TEXT"},
    "edad": {"name": "Edad del paciente", "dataType": "NUMERICAL"},
    "tutor": {"name": "Nombre del tutor", "dataType": "TEXT"},
    "como_se_entero": {"name": "Cómo se enteró", "dataType": "TEXT"},
    "forma_pago": {"name": "Forma de pago", "dataType": "SINGLE_OPTIONS",
                   "options": {"contado": "Contado", "msi": "Meses sin intereses", "plan_pagos": "Plan de pagos"}},
    "canal": {"name": "Canal de origen", "dataType": "TEXT"},
    "chat_id": {"name": "ID de chat del bot", "dataType": "TEXT"},
}


def field_options(key: str) -> dict[str, str]:
    if key == "motivo_consulta":
        from app.clinic_profile import MOTIVOS

        return dict(MOTIVOS)
    return dict(CUSTOM_FIELDS[key].get("options") or {})


_fields_cache: dict[str, Any] = {"at": 0.0, "ids": {}}


async def list_custom_fields() -> list[dict[str, Any]]:
    s = get_settings()
    body = await _request(
        "GET", f"/locations/{s.ghl_location_id}/customFields", params={"model": "contact"}
    )
    return body.get("customFields") or []


async def custom_field_ids() -> dict[str, str]:
    """Mapa clave de dominio -> id del campo en GHL (cache 10 min)."""
    if _fields_cache["ids"] and time.time() - _fields_cache["at"] < 600:
        return _fields_cache["ids"]
    by_name = {f.get("name"): f.get("id") for f in await list_custom_fields()}
    ids = {k: by_name[d["name"]] for k, d in CUSTOM_FIELDS.items() if by_name.get(d["name"])}
    _fields_cache.update({"at": time.time(), "ids": ids})
    return ids


def build_custom_fields(values: dict[str, Any], ids: dict[str, str]) -> list[dict[str, Any]]:
    """Traduce valores de dominio a la lista `customFields` de GHL."""
    out: list[dict[str, Any]] = []
    for key, raw in values.items():
        if key not in CUSTOM_FIELDS or key not in ids or raw in (None, ""):
            continue
        value: Any = raw
        opts = field_options(key)
        if opts:
            value = opts.get(str(raw), str(raw))
        elif CUSTOM_FIELDS[key]["dataType"] == "NUMERICAL":
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
        out.append({"id": ids[key], "field_value": value})
    return out
