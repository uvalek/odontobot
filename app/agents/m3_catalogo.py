"""M3 — Catálogo de servicios y precios. Llama tool `buscar_servicios` (perfil de la clínica)."""

from __future__ import annotations

import asyncio
import json

from openai import OpenAI

from app.clinic_profile import NAME as CLINIC_NAME
from app.config import get_settings
from app.llm import completion_params
from app.security.system_prompt import secure_system_prompt
from app.tools.servicios import buscar_servicios

_SYSTEM = secure_system_prompt("m3_catalogo")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_servicios",
            "description": (
                f"Busca servicios y precios de referencia de {CLINIC_NAME}. "
                "Acepta un único parámetro 'busqueda' con palabras clave."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "busqueda": {
                        "type": "string",
                        "description": "Palabras clave del paciente (tratamiento, sinónimo, categoría).",
                    }
                },
                "required": ["busqueda"],
            },
        },
    }
]


def _client() -> OpenAI:
    s = get_settings()
    return OpenAI(
        api_key=s.openai_api_key, timeout=s.openai_timeout_seconds, max_retries=s.openai_max_retries
    )


async def respond(user_text: str, history: list[dict[str, str]]) -> str:
    s = get_settings()
    msgs: list[dict] = [{"role": "system", "content": _SYSTEM}]
    msgs.extend(history[-15:])
    msgs.append({"role": "user", "content": user_text})

    for _ in range(4):  # máximo 4 vueltas de tool calling
        def _call() -> dict:
            resp = _client().chat.completions.create(
                model=s.openai_model_catalog,
                messages=msgs,  # type: ignore[arg-type]
                tools=_TOOLS,  # type: ignore[arg-type]
                **completion_params(
                    s.openai_model_catalog, temperature=0.1, has_tools=True
                ),
            )
            return resp.choices[0].message.model_dump()

        choice = await asyncio.to_thread(_call)
        msgs.append(choice)
        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            return choice.get("content") or ""

        for tc in tool_calls:
            args = json.loads(tc["function"]["arguments"] or "{}")
            results = await buscar_servicios(args.get("busqueda", ""))
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(results, ensure_ascii=False),
                }
            )

    return ""
