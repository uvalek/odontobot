"""M2 — Calificación y agendamiento de citas. Tool calling con la agenda (GoHighLevel o Cal.com) + CRM `contactos`."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.config import get_settings
from app.llm import completion_params
from app.security.system_prompt import secure_system_prompt
from app.tools import agenda, cal, contactos

_SYSTEM = secure_system_prompt("m2_agendamiento")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_disponibilidad",
            "description": "Consulta horarios disponibles en la agenda de la clínica entre dos fechas ISO UTC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "startTime": {"type": "string", "description": "ISO 8601 UTC"},
                    "endTime": {"type": "string", "description": "ISO 8601 UTC"},
                },
                "required": ["startTime", "endTime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Reserva la cita en la agenda de la clínica y guarda al paciente en el CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "startTime": {"type": "string"},
                    "userName": {"type": "string", "description": "Nombre completo del paciente."},
                    "userEmail": {"type": "string"},
                    "motivo_consulta": {
                        "type": "string",
                        "enum": [
                            "dolor_urgencia",
                            "limpieza_revision",
                            "estetica_blanqueamiento",
                            "ortodoncia",
                            "implantes_protesis",
                            "odontopediatria",
                            "",
                        ],
                        "description": "Categoría del motivo de consulta (nunca un diagnóstico).",
                    },
                    "nivel_urgencia": {
                        "type": "string",
                        "enum": ["alta", "media", "baja", ""],
                        "description": "alta = dolor fuerte, golpe o inflamación.",
                    },
                    "tipo_paciente": {
                        "type": "string",
                        "enum": ["nuevo", "seguimiento", ""],
                    },
                    "disponibilidad_preferida": {
                        "type": "string",
                        "description": "Día y rango de horario que prefirió el paciente (ej. martes por la tarde).",
                    },
                    "userPhone": {
                        "type": "string",
                        "description": "Telefono del usuario en formato E.164 (ej: +5215512345678). OBLIGATORIO en Telegram, Instagram y Messenger porque no lo tenemos automatico. En WhatsApp omitelo.",
                    },
                },
                "required": [
                    "startTime",
                    "userName",
                    "userEmail",
                    "motivo_consulta",
                    "nivel_urgencia",
                    "tipo_paciente",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cambioCita",
            "description": "Reagenda o cancela una cita ya existente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "objetivo": {"type": "string", "enum": ["reagendar", "cancelar"]},
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "rescheduleDate": {"type": "string"},
                    "cancelDate": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["objetivo", "email", "reason"],
            },
        },
    },
]


def _client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def _now_cdmx() -> str:
    dt = datetime.now(ZoneInfo("America/Mexico_City"))
    return dt.strftime("%A %d de %B de %Y, %I:%M %p")


async def _cambio_cita(args: dict, user_phone: str) -> dict:
    bookings = await agenda.list_bookings(args["email"])
    if not bookings:
        return {"status": "not_found", "message": "No se encontró ninguna cita."}

    target = bookings[0]
    if len(bookings) > 1 and args.get("rescheduleDate"):
        for b in bookings:
            if args["rescheduleDate"][:10] in (b.get("startTime") or ""):
                target = b
                break
    uid = target.get("uid")

    if args["objetivo"] == "reagendar":
        result = await agenda.reschedule(uid, args["rescheduleDate"])
        return {"status": "rescheduled", "raw": result}
    result = await agenda.cancel(uid, args.get("reason", ""))
    return {"status": "cancelled", "raw": result}


_PHONE_INSTRUCTION_STEP_AUTO = (
    "\nNO pidas número de teléfono. Ya lo tenemos registrado automáticamente desde WhatsApp."
)
_PHONE_INSTRUCTION_RULE_AUTO = (
    "- NUNCA pidas número de teléfono, ya lo tenemos desde WhatsApp"
)
_PHONE_INSTRUCTION_STEP_ASK = (
    "\n3. Número de celular a 10 dígitos (ej: 2414568392). NO pidas LADA ni el +52: "
    "nosotros le ponemos el +52 automáticamente. Es obligatorio para que recepción "
    "pueda confirmar la cita."
)
_PHONE_INSTRUCTION_RULE_ASK = (
    "- Pide el celular SOLO a 10 dígitos (ej: 2414568392). NUNCA pidas LADA "
    "internacional, código de país ni el +52; el sistema lo agrega solo.\n"
    "- Pásalo a book_appointment como `userPhone` tal cual lo dio el usuario "
    "(los 10 dígitos); el servidor le antepone el +52.\n"
    "- Solo si da menos de 10 dígitos, pídelo de nuevo amablemente."
)


def _build_system(canal: str) -> str:
    """Inyecta las instrucciones de teléfono según el canal del usuario."""
    base = _SYSTEM.replace("{{NOW_CDMX}}", _now_cdmx())
    if canal == "whatsapp":
        step = _PHONE_INSTRUCTION_STEP_AUTO
        rule = _PHONE_INSTRUCTION_RULE_AUTO
    else:  # telegram, instagram, messenger
        step = _PHONE_INSTRUCTION_STEP_ASK
        rule = _PHONE_INSTRUCTION_RULE_ASK
    return base.replace("{{PHONE_INSTRUCTION_STEP}}", step).replace(
        "{{PHONE_INSTRUCTION_RULE}}", rule
    )


async def respond(
    user_text: str,
    history: list[dict[str, str]],
    *,
    user_phone: str = "",
    chat_id: str = "",
    canal: str = "",
) -> str:
    s = get_settings()
    system = _build_system(canal)
    msgs: list[dict] = [{"role": "system", "content": system}]
    msgs.extend(history[-15:])
    msgs.append({"role": "user", "content": user_text})

    for _ in range(6):
        def _call() -> dict:
            resp = _client().chat.completions.create(
                model=s.openai_model_brain,
                messages=msgs,  # type: ignore[arg-type]
                tools=_TOOLS,  # type: ignore[arg-type]
                **completion_params(
                    s.openai_model_brain, temperature=0.7, has_tools=True
                ),
            )
            return resp.choices[0].message.model_dump()

        choice = await asyncio.to_thread(_call)
        msgs.append(choice)
        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            return choice.get("content") or ""

        for tc in tool_calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            try:
                if name == "consultar_disponibilidad":
                    result = await agenda.get_slots(args["startTime"], args["endTime"])
                elif name == "book_appointment":
                    # Telefono efectivo: el del canal (WA) > el que pidio el LLM (TG/IG/MSG).
                    effective_phone = user_phone or (
                        cal._normalize_phone(args.get("userPhone")) or ""
                    )
                    booking = await agenda.book(
                        start_time=args["startTime"],
                        user_name=args["userName"],
                        user_email=args["userEmail"],
                        user_phone=effective_phone,
                        chat_id=chat_id,
                        canal=canal,
                        extra={
                            k: args.get(k)
                            for k in (
                                "motivo_consulta",
                                "nivel_urgencia",
                                "tipo_paciente",
                                "disponibilidad_preferida",
                            )
                        },
                    )
                    try:
                        await contactos.upsert_contacto(
                            nombre=args["userName"],
                            correo=args["userEmail"],
                            telefono=effective_phone or None,
                            motivo_consulta=args.get("motivo_consulta") or None,
                            nivel_urgencia=args.get("nivel_urgencia") or None,
                            tipo_paciente=args.get("tipo_paciente") or None,
                            disponibilidad_preferida=(
                                args.get("disponibilidad_preferida") or None
                            ),
                            fecha_cita_iso=contactos.fecha_cita_from_iso_utc(
                                args["startTime"]
                            ),
                            chat_id=chat_id or None,
                            canal=canal or None,
                        )
                    except Exception as e:  # noqa: BLE001
                        booking["contactos_error"] = str(e)
                    result = booking
                elif name == "cambioCita":
                    result = await _cambio_cita(args, user_phone)
                else:
                    result = {"error": f"unknown tool {name}"}
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}

            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    return ""
