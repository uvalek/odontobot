"""Sincroniza al paciente de Supabase (`contactos`) con un contacto de GoHighLevel.

Supabase sigue siendo la fuente del bot (memoria, toggle, datos extraídos).
Cada vez que cambian los datos del paciente se refleja en GHL:
  - nombre, correo, teléfono
  - campos personalizados (motivo, urgencia, tipo de paciente, etc.)
  - etiquetas (`chatbot`, canal, `cita-agendada`, `handoff-doctor`)

El contacto de GHL se crea en cuanto el paciente es identificable (nombre,
correo o teléfono) o cuando se fuerza (handoff). Su id se guarda en
`contactos.ghl_contact_id` para no duplicarlo.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.db import supabase
from app.tools import ghl

log = structlog.get_logger(__name__)

BASE_TAG = "chatbot"
SOURCE = "Chatbot Swiss Dental"


async def _row(chat_id: str) -> dict[str, Any] | None:
    res = await asyncio.to_thread(
        lambda: supabase().table("contactos").select("*").eq("chat_id", chat_id).limit(1).execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _domain_values(row: dict[str, Any] | None) -> dict[str, Any]:
    """Traduce una fila de `contactos` a valores de dominio (inverso de LEAD_COLUMNS)."""
    from app.tools.contactos import (
        BOT_NOTES_PREFIX,
        LEAD_COLUMNS,
        NOTE_FIELDS,
        NOTES_COLUMN,
        _parse_bot_notes,
    )

    if not row:
        return {}
    out: dict[str, Any] = {}
    for key, col in LEAD_COLUMNS.items():
        if row.get(col) not in (None, ""):
            out[key] = row[col]
    notes = row.get(NOTES_COLUMN) or ""
    if notes.startswith(BOT_NOTES_PREFIX):
        by_label = {label: key for key, label in NOTE_FIELDS.items()}
        for label, value in _parse_bot_notes(notes).items():
            if label in by_label:
                out[by_label[label]] = value
    return out


async def _save_ghl_id(row_id: Any, chat_id: str, canal: str, contact_id: str) -> None:
    if row_id is not None:
        q = supabase().table("contactos").update({"ghl_contact_id": contact_id}).eq("id", row_id)
    else:
        q = supabase().table("contactos").insert(
            {"chat_id": chat_id, "canal": canal, "ghl_contact_id": contact_id, "etapa_seguimiento": "nuevo"}
        )
    await asyncio.to_thread(lambda: q.execute())


_FIELD_LABEL = {"phone": "El teléfono", "email": "El correo"}


async def _update_skipping_duplicates(contact_id: str, **fields: Any) -> list[str]:
    """Actualiza el contacto. Si GHL rechaza un teléfono o correo porque ya
    pertenece a otro contacto, reintenta sin ese dato y devuelve una nota
    explicándolo (la cita no debe fallar por eso)."""
    notes: list[str] = []
    arg_by_field = {"phone": "telefono", "email": "correo"}
    for _ in range(3):
        try:
            await ghl.update_contact(contact_id, **fields)
            return notes
        except ghl.GHLError as e:
            field = e.duplicate_field
            arg = arg_by_field.get(field or "")
            if not arg or not fields.get(arg):
                raise
            other = (e.data.get("meta") or {}).get("contactName") or "otro contacto"
            notes.append(
                f"{_FIELD_LABEL[field]} que dio el paciente en el chat ({fields[arg]}) ya pertenece "
                f"al contacto \"{other}\" en GHL; no se agregó a este contacto."
            )
            log.warning("ghl_duplicate_skipped", contact_id=contact_id, field=field)
            fields = {**fields, arg: None}
    return notes


async def sync_contact(
    *,
    chat_id: str,
    canal: str | None = None,
    values: dict[str, Any] | None = None,
    tags: list[str] | tuple[str, ...] = (),
    force: bool = False,
    note: str | None = None,
) -> str | None:
    """Crea/actualiza el contacto en GHL. Devuelve su id o None si no aplica.

    `values` (nombres de dominio) tiene prioridad sobre lo guardado en Supabase.
    Nunca levanta excepción: los errores se registran y el bot sigue.
    """
    if not ghl.enabled() or not chat_id:
        return None
    try:
        row = await _row(chat_id)
        data = {**_domain_values(row), **{k: v for k, v in (values or {}).items() if v not in (None, "")}}
        canal = canal or (row or {}).get("canal") or ""
        data.setdefault("canal", canal)
        data["chat_id"] = chat_id

        nombre, correo, telefono = data.get("nombre"), data.get("correo"), data.get("telefono")
        contact_id = (row or {}).get("ghl_contact_id")

        try:
            field_ids = await ghl.custom_field_ids()
        except ghl.GHLError as e:
            log.warning("ghl_custom_fields_unavailable", error=str(e)[:200])
            field_ids = {}
        custom = ghl.build_custom_fields(data, field_ids)

        new_contact = False
        conflict_notes: list[str] = []
        if contact_id:
            conflict_notes = await _update_skipping_duplicates(
                contact_id, nombre=nombre, correo=correo, telefono=telefono, custom_fields=custom
            )
        elif correo or telefono:
            try:
                contact = await ghl.upsert_contact(
                    nombre=nombre, correo=correo, telefono=telefono, custom_fields=custom, source=SOURCE
                )
            except ghl.GHLError as e:
                # Correo y teléfono pertenecen a contactos distintos: se crea con
                # el correo (o el teléfono) y se deja nota del dato que choca.
                field = e.duplicate_field
                if field not in ("phone", "email") or not (correo and telefono):
                    raise
                keep_email = field == "phone"
                other = (e.data.get("meta") or {}).get("contactName") or "otro contacto"
                dropped = telefono if keep_email else correo
                conflict_notes.append(
                    f"{_FIELD_LABEL[field]} que dio el paciente en el chat ({dropped}) ya pertenece "
                    f"al contacto \"{other}\" en GHL; no se agregó a este contacto."
                )
                contact = await ghl.upsert_contact(
                    nombre=nombre,
                    correo=correo if keep_email else None,
                    telefono=None if keep_email else telefono,
                    custom_fields=custom,
                    source=SOURCE,
                )
            contact_id, new_contact = contact.get("id"), True
        elif nombre or force:
            contact = await ghl.create_contact(
                nombre=nombre or f"Visitante {canal or 'web'} {chat_id[-6:]}",
                custom_fields=custom,
                source=SOURCE,
            )
            contact_id, new_contact = contact.get("id"), True
        else:
            return None  # todavía no hay forma de identificar al paciente

        if not contact_id:
            return None
        if new_contact:
            await _save_ghl_id((row or {}).get("id"), chat_id, canal, contact_id)
        all_tags = [t for t in ([BASE_TAG, canal] if new_contact else []) + list(tags) if t]
        if all_tags:
            await ghl.add_tags(contact_id, all_tags)
        if force and conflict_notes:
            note = "\n".join([*conflict_notes, *( [note] if note else [] )])
        if note:
            await ghl.add_note(contact_id, note)
        log.info("ghl_contact_synced", chat_id=chat_id, contact_id=contact_id, new=new_contact, tags=all_tags)
        return contact_id
    except Exception as e:  # noqa: BLE001
        log.warning("ghl_sync_failed", chat_id=chat_id, error=str(e)[:300])
        return None
