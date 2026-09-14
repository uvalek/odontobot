"""Saneado de la respuesta del modelo antes de enviarla al usuario.

No queremos:
- Que el bot reproduzca textualmente fragmentos identificables del system
  prompt (los pone disponibles para ataques de re-uso).
- Que filtre claves, tokens o emails internos.
- Que devuelva HTML/JS que rompa el frontend.
- Que supere el límite de longitud por chunk de los canales (WhatsApp:
  4096 chars, Telegram: 4096, Instagram/Messenger menos generosos).

Si encontramos una señal de fuga, **reemplazamos** el chunk por un mensaje
neutro y loggeamos como evento de seguridad. Mejor un mensaje genérico que
una respuesta peligrosa.
"""

from __future__ import annotations

import re

from app.clinic_profile import NAME as CLINIC_NAME

# Frases sentinela: si aparecen en la respuesta del modelo, casi seguro
# está repitiendo (o describiendo) su system prompt. Lista intencionalmente
# corta para no tener falsos positivos.
_LEAK_SENTINELS: list[re.Pattern[str]] = [
    re.compile(r"REGLAS\s+(QUE\s+)?NUNCA\s+DEBES\s+ROMPER", re.I),
    re.compile(r"<\s*user_message\s*>", re.I),
    re.compile(r"</\s*user_message\s*>", re.I),
    re.compile(r"\bsystem\s+prompt\b", re.I),
    re.compile(r"PROMPT\s+DEL\s+SISTEMA", re.I),
    re.compile(r"\bTU\s+ROL\s*:\s*\n?\s*Eres", re.I),
    re.compile(r"FENCE_HEADER", re.I),
    # Claves/tokens visibles (formato común)
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),  # Google API keys
]

# Etiquetas HTML potencialmente peligrosas. Las dejamos crudas para que el
# widget/canal las muestre como texto, pero rompemos `<script` por si
# escapa el render.
_SCRIPT_RE = re.compile(r"<\s*/?\s*script\b", re.I)

# Límite duro por chunk (WhatsApp/Telegram aceptan 4096; nos quedamos
# un poco abajo para incluir captions/cabeceras).
MAX_CHUNK_LEN = 3800

NEUTRAL_REPLACEMENT = (
    f"No tengo permitido responder a eso. ¿Hay algo de {CLINIC_NAME} en lo que "
    "te pueda ayudar?"
)


def sanitize_chunk(text: str) -> tuple[str, list[str]]:
    """Limpia un chunk individual y devuelve (chunk_seguro, motivos)."""
    if not text:
        return "", []
    motivos: list[str] = []

    # Detección de fuga
    for pat in _LEAK_SENTINELS:
        if pat.search(text):
            motivos.append(f"leak_match:{pat.pattern[:40]}")
            return NEUTRAL_REPLACEMENT, motivos

    # Neutraliza <script>
    cleaned = _SCRIPT_RE.sub("[script_removido]", text)
    if cleaned != text:
        motivos.append("script_tag_stripped")

    # Límite de longitud
    if len(cleaned) > MAX_CHUNK_LEN:
        cleaned = cleaned[: MAX_CHUNK_LEN - 30].rstrip() + " […]"
        motivos.append("truncated")

    return cleaned, motivos


def sanitize_chunks(chunks: list[str]) -> tuple[list[str], list[str]]:
    """Aplica `sanitize_chunk` a cada elemento. Devuelve (chunks, motivos_globales)."""
    out: list[str] = []
    all_motivos: list[str] = []
    for c in chunks:
        sc, motivos = sanitize_chunk(c)
        if sc:
            out.append(sc)
        all_motivos.extend(motivos)
    if not out:
        # Si saneamos hasta quedar vacío, manda al menos el neutral.
        out = [NEUTRAL_REPLACEMENT]
    return out, all_motivos
