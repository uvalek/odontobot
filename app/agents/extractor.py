"""Extractor de datos del paciente.

Despues de cada turno corre un mini-LLM con structured output sobre los
ultimos N mensajes para extraer (nombres de dominio; la traduccion a columnas
de `contactos` vive en `app/tools/contactos.py::LEAD_COLUMNS`):

  - nombre, correo, telefono
  - motivo_consulta ("dolor_urgencia" | "limpieza_revision" | "estetica_blanqueamiento"
                     | "ortodoncia" | "implantes_protesis" | "odontopediatria")
  - edad (entero)
  - forma_pago ("contado" | "msi" | "plan_pagos")
  - nivel_urgencia ("alta" | "media" | "baja")
  - tipo_paciente ("nuevo" | "seguimiento")
  - disponibilidad_preferida, tutor, como_se_entero (texto corto)
  - etapa_sugerida ("nuevo" | "calificado" | "cita_agendada" | "atendido")

Devuelve solo los campos que pudo inferir con alta confianza. Los None se
ignoran y NO sobrescriben datos previos en `contactos`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from openai import OpenAI

from app.clinic_profile import MOTIVOS
from app.config import get_settings
from app.llm import completion_params

log = structlog.get_logger(__name__)


_SYSTEM = """Eres un extractor de datos para el CRM de una clinica dental en Mexico.
Lees el ultimo mensaje del paciente y, opcionalmente, el historial reciente,
y devuelves un JSON con los datos que puedas inferir con alta confianza.

REGLAS:
- Si un campo NO esta claro, devuelvelo como null. Mejor null que adivinar.
- NUNCA extraigas sintomas detallados, diagnosticos, medicamentos ni historial clinico.
- motivo_consulta: una de ["dolor_urgencia","limpieza_revision","estetica_blanqueamiento",
  "ortodoncia","implantes_protesis","odontopediatria"]. Dolor, golpe, inflamacion o diente
  roto -> "dolor_urgencia". Brackets o alineadores -> "ortodoncia". Coronas, implantes,
  protesis -> "implantes_protesis". Revision de un nino -> "odontopediatria". null si dudas.
- edad: entero con la edad del paciente (no del tutor). null si no la dijo.
- forma_pago: una de ["contado","msi","plan_pagos"]. Efectivo, tarjeta de un solo pago o
  transferencia -> "contado". Meses sin intereses -> "msi". Enganche y mensualidades ->
  "plan_pagos". null si dudas.
- nivel_urgencia: "alta" si hay dolor fuerte, golpe, inflamacion o sangrado; "media" si hay
  molestia leve; "baja" si dijo que no tiene dolor. null si no se hablo de eso.
- tipo_paciente: "nuevo" si es su primera vez en la clinica; "seguimiento" si ya se atendio
  antes. null si dudas.
- disponibilidad_preferida: texto corto con dia y horario preferido (ej. "martes por la tarde").
- tutor: nombre de la madre, padre o tutor, solo si el paciente es menor y lo dijo.
- como_se_entero: texto corto (ej. "Instagram", "recomendacion", "Google").
- etapa_sugerida: heuristica conservadora.
    * "nuevo": acaba de llegar, solo saludo
    * "calificado": ya dio motivo_consulta + nivel_urgencia + tipo_paciente
    * "cita_agendada": confirmo fecha y hora de su cita
    * "atendido": dijo que ya fue a su cita
  Si no estas seguro, null (no degrades).
- nombre: solo si el paciente lo dijo explicitamente ("me llamo Juan", "soy Maria").
- correo, telefono: solo si el paciente los escribio en el mensaje.

Responde SIEMPRE con un JSON valido con exactamente estas llaves:
{"nombre": str|null, "correo": str|null, "telefono": str|null,
 "motivo_consulta": str|null, "edad": int|null, "forma_pago": str|null,
 "nivel_urgencia": str|null, "tipo_paciente": str|null,
 "disponibilidad_preferida": str|null, "tutor": str|null,
 "como_se_entero": str|null, "etapa_sugerida": str|null}
"""


_VALID_ENUMS: dict[str, set[str]] = {
    "motivo_consulta": set(MOTIVOS),
    "forma_pago": {"contado", "msi", "plan_pagos"},
    "nivel_urgencia": {"alta", "media", "baja"},
    "tipo_paciente": {"nuevo", "seguimiento"},
}
_VALID_ETAPA = {"nuevo", "calificado", "cita_agendada", "atendido"}
_TEXT_FIELDS = (
    "nombre",
    "correo",
    "telefono",
    "disponibilidad_preferida",
    "tutor",
    "como_se_entero",
)


def _client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Normaliza y descarta valores invalidos o vacios."""
    out: dict[str, Any] = {}
    for k in _TEXT_FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    edad = raw.get("edad")
    if isinstance(edad, (int, float)) and 0 < edad < 120:
        out["edad"] = int(edad)
    for k, valid in _VALID_ENUMS.items():
        v = raw.get(k)
        if isinstance(v, str) and v.lower().strip() in valid:
            out[k] = v.lower().strip()
    et = raw.get("etapa_sugerida")
    if isinstance(et, str) and et.lower().strip() in _VALID_ETAPA:
        out["etapa_seguimiento"] = et.lower().strip()
    return out


async def extract(user_text: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Devuelve un dict con los campos detectados (solo los no nulos)."""
    if not user_text or not user_text.strip():
        return {}

    settings = get_settings()
    msgs: list[dict[str, str]] = [{"role": "system", "content": _SYSTEM}]
    if history:
        msgs.extend(history[-6:])
    msgs.append({"role": "user", "content": user_text})

    def _do() -> str:
        resp = _client().chat.completions.create(
            model=settings.openai_model_brain,
            messages=msgs,  # type: ignore[arg-type]
            response_format={"type": "json_object"},
            **completion_params(
                settings.openai_model_brain, temperature=0, max_tokens=300
            ),
        )
        return resp.choices[0].message.content or "{}"

    try:
        raw = await asyncio.to_thread(_do)
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("extractor_failed", error=str(e))
        return {}

    if not isinstance(data, dict):
        return {}

    return _coerce(data)
