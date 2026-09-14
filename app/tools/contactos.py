"""Upsert de leads (pacientes) en la tabla `contactos` de Supabase (CRM propio).

DEMO — mapeo semántico sobre columnas existentes (Opción A). Los agentes y el
extractor hablan en términos dentales; la traducción a columnas vive SOLO en
`LEAD_COLUMNS` y `NOTE_FIELDS`:

  motivo_consulta  -> zona_interes      (categoría del motivo)
  edad             -> presupuesto_max   (numeric)
  forma_pago       -> tipo_credito      (contado | msi | plan_pagos)
  fecha_cita       -> fecha_visita      (timestamptz)

Los datos sin columna propia (urgencia, tipo de paciente, disponibilidad,
tutor, cómo se enteró) se guardan en `notas_internas` como una línea que
empieza con `[Bot]`. El bot solo reescribe esa línea mientras las notas sigan
siendo suyas; si el personal de la clínica las edita, no las toca.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog

from app.db import supabase
from app.tools import ghl_sync

log = structlog.get_logger(__name__)

# campo de dominio -> columna de `contactos`
LEAD_COLUMNS: dict[str, str] = {
    "nombre": "nombre",
    "correo": "correo",
    "telefono": "telefono",
    "motivo_consulta": "zona_interes",
    "edad": "presupuesto_max",
    "forma_pago": "tipo_credito",
    "fecha_cita": "fecha_visita",
    "etapa_seguimiento": "etapa_seguimiento",
}

# campo de dominio -> etiqueta dentro de la línea [Bot] de notas_internas
NOTE_FIELDS: dict[str, str] = {
    "nivel_urgencia": "urgencia",
    "tipo_paciente": "paciente",
    "disponibilidad_preferida": "disponibilidad",
    "tutor": "tutor",
    "como_se_entero": "origen",
}

NOTES_COLUMN = "notas_internas"
BOT_NOTES_PREFIX = "[Bot]"
_NOTES_SEP = " · "


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _split_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Separa campos de dominio en (columnas, notas)."""
    cols: dict[str, Any] = {}
    notes: dict[str, str] = {}
    for k, v in fields.items():
        if _empty(v):
            continue
        if k in LEAD_COLUMNS:
            cols[LEAD_COLUMNS[k]] = v
        elif k in NOTE_FIELDS:
            notes[NOTE_FIELDS[k]] = str(v).strip()
    return cols, notes


def _render_notes(notes: dict[str, str]) -> str:
    parts = [f"{label}: {notes[label]}" for label in NOTE_FIELDS.values() if label in notes]
    return f"{BOT_NOTES_PREFIX} " + _NOTES_SEP.join(parts)


def _parse_bot_notes(text: str) -> dict[str, str]:
    body = text[len(BOT_NOTES_PREFIX):].strip()
    out: dict[str, str] = {}
    for part in body.split(_NOTES_SEP):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def merge_notes(current: str | None, new: dict[str, str]) -> str | None:
    """Devuelve el nuevo valor de notas_internas o None si no hay que escribir.

    - Vacías: se crea la línea [Bot].
    - Empiezan con [Bot]: se combinan (lo nuevo actualiza lo anterior).
    - Editadas por una persona: no se tocan.
    """
    if not new:
        return None
    if _empty(current):
        return _render_notes(new)
    assert current is not None
    if not current.startswith(BOT_NOTES_PREFIX):
        return None
    merged = {**_parse_bot_notes(current), **new}
    rendered = _render_notes(merged)
    return rendered if rendered != current else None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


async def _find_contacto(chat_id: str | None, correo: str | None) -> dict[str, Any] | None:
    """Busca la fila del paciente: primero por chat_id (la crea el webhook),
    luego por correo."""
    select = f"id, handle, {NOTES_COLUMN}"
    for col, val in (("chat_id", chat_id), ("correo", correo)):
        if not val:
            continue
        res = await asyncio.to_thread(
            lambda col=col, val=val: (
                supabase()
                .table("contactos")
                .select(select)
                .eq(col, val)
                .limit(1)
                .execute()
            )
        )
        rows = res.data or []
        if rows:
            return rows[0]
    return None


async def upsert_contacto(
    *,
    nombre: str,
    correo: str,
    telefono: str | None = None,
    motivo_consulta: str | None = None,
    nivel_urgencia: str | None = None,
    tipo_paciente: str | None = None,
    disponibilidad_preferida: str | None = None,
    fecha_cita_iso: str | None = None,
    etapa_seguimiento: str = "cita_agendada",
    chat_id: str | None = None,
    canal: str | None = None,
) -> dict[str, Any]:
    """Guarda al paciente al confirmar una cita (llamado desde M2)."""
    cols, notes = _split_fields(
        {
            "nombre": nombre,
            "correo": correo,
            "telefono": telefono,
            "motivo_consulta": motivo_consulta,
            "fecha_cita": fecha_cita_iso,
            "etapa_seguimiento": etapa_seguimiento,
            "nivel_urgencia": nivel_urgencia,
            "tipo_paciente": tipo_paciente,
            "disponibilidad_preferida": disponibilidad_preferida,
        }
    )
    payload: dict[str, Any] = dict(cols)
    if chat_id:
        payload["chat_id"] = chat_id
    if canal:
        payload["canal"] = canal

    # En WhatsApp el telefono es el mejor identificador legible para el dashboard.
    handle_candidate = telefono if (canal == "whatsapp" and telefono) else None

    existing = await _find_contacto(chat_id, correo)
    if existing:
        cid = existing["id"]
        if handle_candidate and not (existing.get("handle") or "").strip():
            payload["handle"] = handle_candidate
        new_notes = merge_notes(existing.get(NOTES_COLUMN), notes)
        if new_notes is not None:
            payload[NOTES_COLUMN] = new_notes
        res = await asyncio.to_thread(
            lambda: (
                supabase()
                .table("contactos")
                .update(payload)
                .eq("id", cid)
                .execute()
            )
        )
        log.info("contacto_actualizado", id=cid, correo=correo)
        await ghl_sync.sync_contact(chat_id=chat_id or "", canal=canal, tags=["cita-agendada"])
        return (res.data or [{"id": cid}])[0]

    if handle_candidate:
        payload.setdefault("handle", handle_candidate)
    if notes:
        payload[NOTES_COLUMN] = _render_notes(notes)
    res = await asyncio.to_thread(
        lambda: supabase().table("contactos").insert(payload).execute()
    )
    log.info("contacto_creado", correo=correo)
    await ghl_sync.sync_contact(chat_id=chat_id or "", canal=canal, tags=["cita-agendada"])
    return (res.data or [{}])[0]


async def merge_lead_fields(
    *,
    chat_id: str,
    canal: str,
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """Inserta o actualiza `contactos` por chat_id sin sobrescribir.

    `fields` llega con nombres de dominio (ver extractor). Reglas:
    - Si la fila no existe, la crea con todos los campos provistos.
    - Si existe, solo escribe las columnas cuyo valor actual es NULL/vacio.
      Asi el personal de la clinica puede editar a mano y el bot no le pisa
      los datos. Excepcion: la linea [Bot] de notas_internas (ver merge_notes).
    """
    if not chat_id or not fields:
        return None

    cols, notes = _split_fields(fields)
    if "presupuesto_max" in cols:  # edad
        cols["presupuesto_max"] = _to_float(cols["presupuesto_max"])
        if cols["presupuesto_max"] is None:
            cols.pop("presupuesto_max")
    if not cols and not notes:
        return None

    select_cols = ", ".join(["id", *sorted(set(LEAD_COLUMNS.values())), NOTES_COLUMN])
    existing = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .select(select_cols)
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
    )
    rows = existing.data or []

    if not rows:
        payload: dict[str, Any] = {"chat_id": chat_id, "canal": canal, **cols}
        if notes:
            payload[NOTES_COLUMN] = _render_notes(notes)
        # nombre ya es nullable; el dashboard cae en handle/chat_id como fallback.
        payload.setdefault("etapa_seguimiento", "nuevo")
        res = await asyncio.to_thread(
            lambda: supabase().table("contactos").insert(payload).execute()
        )
        log.info("contacto_lead_creado", chat_id=chat_id, fields=list(payload.keys()))
        await ghl_sync.sync_contact(chat_id=chat_id, canal=canal)
        return (res.data or [{}])[0]

    row = rows[0]
    cid = row["id"]
    update: dict[str, Any] = {}
    for k, v in cols.items():
        if _empty(row.get(k)):
            update[k] = v
    new_notes = merge_notes(row.get(NOTES_COLUMN), notes)
    if new_notes is not None:
        update[NOTES_COLUMN] = new_notes

    if not update:
        return row

    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .update(update)
            .eq("id", cid)
            .execute()
        )
    )
    log.info("contacto_lead_actualizado", chat_id=chat_id, fields=list(update.keys()))
    await ghl_sync.sync_contact(chat_id=chat_id, canal=canal)
    return (res.data or [{"id": cid}])[0]


async def mark_handoff(chat_id: str, canal: str | None = None, nota: str | None = None) -> None:
    """Marca la conversación como pasada al doctor (etapa `handoff`) y lo
    refleja en GoHighLevel con la etiqueta `handoff-doctor` y una nota."""
    if not chat_id:
        return
    etapa = {LEAD_COLUMNS["etapa_seguimiento"]: "handoff"}
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .update(etapa)
            .eq("chat_id", chat_id)
            .execute()
        )
    )
    if not res.data:
        payload = {"chat_id": chat_id, **etapa}
        if canal:
            payload["canal"] = canal
        await asyncio.to_thread(
            lambda: supabase().table("contactos").insert(payload).execute()
        )
    log.info("contacto_handoff", chat_id=chat_id)
    await ghl_sync.sync_contact(
        chat_id=chat_id,
        canal=canal,
        tags=["handoff-doctor"],
        force=True,
        note=f"El chatbot pasó la conversación al doctor. Último mensaje del paciente:\n{nota}" if nota else None,
    )


async def cita_actual(chat_id: str) -> dict[str, Any] | None:
    """Cita futura ya agendada en esta conversación (según Supabase) o None."""
    if not chat_id:
        return None
    res = await asyncio.to_thread(
        lambda: (
            supabase()
            .table("contactos")
            .select("nombre, correo, telefono, etapa_seguimiento, fecha_visita")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
    )
    rows = res.data or []
    if not rows or rows[0].get("etapa_seguimiento") != "cita_agendada" or not rows[0].get("fecha_visita"):
        return None
    row = rows[0]
    try:
        fecha = datetime.fromisoformat(str(row["fecha_visita"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fecha.tzinfo is None or fecha <= datetime.now(fecha.tzinfo):
        return None
    return {**row, "fecha_visita": fecha.isoformat()}


async def actualizar_cita(chat_id: str, *, nueva_fecha_iso: str | None, cancelada: bool = False) -> None:
    """Refleja en Supabase y GHL un reagendado o una cancelación."""
    if not chat_id:
        return
    update: dict[str, Any] = (
        {"fecha_visita": None, LEAD_COLUMNS["etapa_seguimiento"]: "calificado"}
        if cancelada
        else {"fecha_visita": fecha_cita_from_iso_utc(nueva_fecha_iso or ""), LEAD_COLUMNS["etapa_seguimiento"]: "cita_agendada"}
    )
    await asyncio.to_thread(
        lambda: supabase().table("contactos").update(update).eq("chat_id", chat_id).execute()
    )
    log.info("contacto_cita_actualizada", chat_id=chat_id, cancelada=cancelada)
    await ghl_sync.sync_contact(chat_id=chat_id, tags=["cita-cancelada" if cancelada else "cita-reagendada"])


def fecha_cita_from_iso_utc(iso_utc: str) -> str | None:
    """Devuelve un ISO timestamptz que Supabase puede insertar tal cual."""
    if not iso_utc:
        return None
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.isoformat()
    except (TypeError, ValueError):
        return None
