"""Agenda de citas: elige el proveedor de calendario.

- GoHighLevel si están `GHL_PRIVATE_TOKEN`, `GHL_LOCATION_ID` y `GHL_CALENDAR_ID`.
- Cal.com en caso contrario (comportamiento anterior).

Las funciones devuelven el mismo formato para ambos proveedores, así el
agente M2 y su prompt no cambian.
"""

from __future__ import annotations

from typing import Any

from app.tools import cal, ghl, ghl_sync


def provider() -> str:
    return "ghl" if ghl.calendar_enabled() else "cal"


async def get_slots(start_iso: str, end_iso: str) -> dict[str, Any]:
    if provider() == "ghl":
        return await ghl.get_free_slots(start_iso, end_iso)
    return await cal.get_slots(start_iso, end_iso)


async def book(
    *,
    start_time: str,
    user_name: str,
    user_email: str,
    user_phone: str,
    chat_id: str = "",
    canal: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if provider() != "ghl":
        return await cal.book(
            start_time=start_time, user_name=user_name, user_email=user_email, user_phone=user_phone
        )
    contact_id = await ghl_sync.sync_contact(
        chat_id=chat_id,
        canal=canal,
        values={"nombre": user_name, "correo": user_email, "telefono": user_phone, **(extra or {})},
        force=True,
    )
    if not contact_id:
        raise ghl.GHLError("No se pudo crear el contacto del paciente en GoHighLevel")
    appt = await ghl.create_appointment(
        contact_id=contact_id, start_iso=start_time, title=f"Cita - {user_name}"
    )
    return {
        "status": "success",
        "provider": "ghl",
        "appointmentId": appt.get("id"),
        "contactId": contact_id,
        "startTime": appt.get("startTime") or start_time,
    }


async def list_bookings(email: str) -> list[dict[str, Any]]:
    """Citas futuras del paciente normalizadas a [{uid, startTime}]."""
    if provider() != "ghl":
        return await cal.list_bookings(email)
    contact = await ghl.find_contact(correo=email)
    if not contact:
        return []
    return [
        {"uid": ev.get("id"), "startTime": ev.get("startTime")}
        for ev in await ghl.contact_appointments(contact["id"])
    ]


async def reschedule(uid: str, new_start_iso: str) -> dict[str, Any]:
    if provider() != "ghl":
        return await cal.reschedule(uid, new_start_iso)
    return await ghl.reschedule_appointment(uid, new_start_iso)


async def cancel(uid: str, reason: str = "") -> dict[str, Any]:
    if provider() != "ghl":
        return await cal.cancel(uid, reason)
    return await ghl.cancel_appointment(uid)
