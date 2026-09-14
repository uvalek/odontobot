"""Búsqueda de servicios de la clínica (catálogo en `app/clinic_profile.py`).

Mismo contrato que tenía la tool de catálogo anterior: recibe un texto con
palabras clave y devuelve una lista de dicts ordenada por relevancia. No usa
base de datos: el catálogo del demo vive en el perfil de la clínica.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from app.clinic_profile import SERVICES

_STOPWORDS = {
    "que", "qué", "cuanto", "cuánto", "cuesta", "cuestan", "precio", "precios",
    "para", "por", "los", "las", "una", "uno", "unos", "del", "con", "tienen",
    "hacen", "quiero", "info", "informacion", "sale", "costo", "mis", "mas",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _public(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": s["id"],
        "nombre": s["nombre"],
        "categoria": s["categoria"],
        "texto_precio": s["texto_precio"],
        "precio_desde": s["precio"],
        "incluye": s["incluye"],
    }


def search(busqueda: str) -> list[dict[str, Any]]:
    """Versión síncrona (útil en tests)."""
    query = _norm(busqueda or "").strip()
    if not query:
        return [_public(s) for s in SERVICES]

    tokens = [t for t in query.split() if len(t) >= 3 and t not in _STOPWORDS]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, s in enumerate(SERVICES):
        nombre = _norm(s["nombre"])
        aliases = [_norm(a) for a in s["alias"]]
        score = 0
        for alias in aliases:
            if alias in query:
                score += 10
        for t in tokens:
            if t in nombre:
                score += 4
            if any(t in a for a in aliases):
                score += 3
        if score:
            scored.append((score, -idx, s))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [_public(s) for _, _, s in scored]


async def buscar_servicios(busqueda: str) -> list[dict[str, Any]]:
    return search(busqueda)
