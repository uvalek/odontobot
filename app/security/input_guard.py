"""Pre-filtro de mensajes entrantes para detectar prompt injection.

Devuelve tres clasificaciones:
- `SAFE`    : sigue al modelo sin cambios.
- `SUSPICIOUS`: sigue al modelo, pero se loguea con la razón. El sistema
  upstream puede contarlo y subir el riesgo del usuario.
- `BLOCK`   : NO se manda al modelo. Se responde con un mensaje neutro de
  redirección.

Reglas de detección:
1. Caracteres invisibles (Unicode tag chars, zero-width). Se eliminan antes
   de inspeccionar; si quedaba un payload puro escondido, el texto saneado
   es vacío y se bloquea.
2. Longitud excesiva.
3. Patrones conocidos de jailbreak / prompt-leak (regex case-insensitive).
4. Heurística de "muro de instrucciones": densidad alta de verbos
   imperativos típicos del prompt-injection.
5. Densidad sospechosa de caracteres no legibles (probable payload codificado
   base64/hex).

Es una primera capa: no pretende atrapar todo. La defensa real son las capas
de fence (envolver el input como datos) + system prompt robusto + output
guard. Aquí solo recortamos lo obvio.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from app import clinic_profile


class Decision(str, Enum):
    SAFE = "SAFE"
    SUSPICIOUS = "SUSPICIOUS"
    BLOCK = "BLOCK"


# ---------------------------------------------------------------------------
# Sanitización: quita caracteres invisibles y normaliza Unicode.
# ---------------------------------------------------------------------------

# Tag chars usados en ataques de "invisible prompt injection".
_TAG_RANGE = (0xE0000, 0xE007F)
# Zero-width: U+200B..U+200F, U+202A..U+202E (bidi), U+2060..U+2064, U+FEFF.
_ZERO_WIDTH = set(
    list(range(0x200B, 0x2010))
    + list(range(0x202A, 0x202F))
    + list(range(0x2060, 0x2065))
    + [0xFEFF]
)


def sanitize(text: str) -> tuple[str, bool]:
    """Elimina caracteres invisibles y normaliza. Devuelve (texto, hubo_cambios)."""
    if not text:
        return "", False
    # NFC primero para colapsar combinaciones equivalentes.
    text_nfc = unicodedata.normalize("NFC", text)
    out_chars: list[str] = []
    stripped = 0
    for ch in text_nfc:
        cp = ord(ch)
        if _TAG_RANGE[0] <= cp <= _TAG_RANGE[1] or cp in _ZERO_WIDTH:
            stripped += 1
            continue
        # categoría Cf (formato) y algunos Cc (control): fuera, excepto
        # newlines/tabs.
        cat = unicodedata.category(ch)
        if cat == "Cf":
            stripped += 1
            continue
        if cat == "Cc" and ch not in ("\n", "\r", "\t"):
            stripped += 1
            continue
        out_chars.append(ch)
    cleaned = "".join(out_chars)
    return cleaned, stripped > 0 or cleaned != text


# ---------------------------------------------------------------------------
# Patrones de prompt injection / jailbreak.
# ---------------------------------------------------------------------------

# Cada patrón = (regex compilado, etiqueta).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Español
    (re.compile(r"\bignor[ae]\s+(las|tus|todas)\s+(instruc|reglas|el\s+prompt)", re.I), "ignore_instructions_es"),
    (re.compile(r"\bolvid[ae]\s+(lo\s+anterior|tus?\s+instruc|tu\s+rol)", re.I), "forget_role_es"),
    (re.compile(r"\bahora\s+eres\b|\ba\s+partir\s+de\s+ahora\s+actúa\b|\bpretende\s+ser\b|\bsimula\s+ser\b", re.I), "roleplay_es"),
    (re.compile(r"\b(system\s+prompt|prompt\s+del\s+sistema|tus\s+instruc(ciones)?\s+iniciales?)\b", re.I), "system_prompt_mention_es"),
    (re.compile(r"\b(revela|muestra|imprime|dime|enseñame|enséñame|cuéntame)\s+(tu\s+)?(system|prompt|instruc|reglas|configuración)", re.I), "reveal_system_es"),
    (re.compile(r"\bmodo\s+desarrollador\b|\bmodo\s+admin\b|\bjailbreak\b|\bDAN\b|\bdo\s+anything\s+now\b", re.I), "developer_mode"),
    # Inglés
    (re.compile(r"\bignore\s+(all|the|your|previous)\s+(instruc|rules|prompt)", re.I), "ignore_instructions_en"),
    (re.compile(r"\bforget\s+(everything|your\s+instructions|your\s+role|the\s+above)", re.I), "forget_role_en"),
    (re.compile(r"\byou\s+are\s+now\b|\bfrom\s+now\s+on\b|\bpretend\s+to\s+be\b|\bact\s+as\b|\broleplay\s+as\b", re.I), "roleplay_en"),
    (re.compile(r"\b(reveal|show|print|tell\s+me)\s+(your\s+)?(system|prompt|instruc|initial|rules)", re.I), "reveal_system_en"),
    # Intentos de ejecución de código
    (re.compile(r"```\s*(python|bash|sh|js|javascript|sql)\b", re.I), "code_fence"),
    (re.compile(r"\b(exec|eval)\s*\(", re.I), "code_call"),
    (re.compile(r"<\s*script\b", re.I), "html_script"),
    # Cerrar el fence del sistema (si alguien intenta romper el wrapping).
    (re.compile(r"</?user_message\s*>", re.I), "fence_tag"),
    (re.compile(r"</?system\s*>", re.I), "system_tag"),
    # Extracción de secretos / configuración
    (re.compile(r"\b(api[_\s-]?key|token|secret|password|env\s+var)\b", re.I), "secret_keyword"),
]


@dataclass
class GuardResult:
    decision: Decision
    sanitized: str
    reasons: list[str]
    invisible_chars_stripped: bool

    @property
    def is_block(self) -> bool:
        return self.decision is Decision.BLOCK

    @property
    def is_suspicious(self) -> bool:
        return self.decision is Decision.SUSPICIOUS


# Umbrales
MAX_LEN = 2000           # caracteres
MAX_PATTERN_HITS = 1     # 1+ patrones críticos -> SUSPICIOUS
BLOCK_PATTERN_HITS = 2   # 2+ patrones críticos -> BLOCK
LOW_LEGIBLE_THRESHOLD = 0.55  # < 55% caracteres legibles -> SUSPICIOUS


_LEGIBLE_RE = re.compile(r"[\w\s.,;:¡!¿?'\"@/#&%()\-+áéíóúÁÉÍÓÚñÑüÜ\n\t]")


def _legible_ratio(text: str) -> float:
    if not text:
        return 1.0
    legible = sum(1 for ch in text if _LEGIBLE_RE.match(ch))
    return legible / max(1, len(text))


def classify(text: str) -> GuardResult:
    """Clasifica un mensaje. No tiene efectos secundarios — el llamador
    decide qué loguear o cómo responder."""
    sanitized, had_invisible = sanitize(text or "")

    if not sanitized.strip():
        # Mensaje vacío después de saneo: si traía caracteres invisibles,
        # alguien puso un payload escondido — bloquea.
        if had_invisible:
            return GuardResult(
                Decision.BLOCK, sanitized, ["empty_after_strip"], True
            )
        # Vacío plano: lo dejamos pasar como SAFE; el caller decide ignorarlo.
        return GuardResult(Decision.SAFE, sanitized, [], False)

    reasons: list[str] = []

    if len(sanitized) > MAX_LEN:
        reasons.append("too_long")

    for pat, tag in _PATTERNS:
        if pat.search(sanitized):
            reasons.append(tag)

    ratio = _legible_ratio(sanitized)
    if ratio < LOW_LEGIBLE_THRESHOLD:
        reasons.append(f"low_legible_ratio:{ratio:.2f}")

    decision = Decision.SAFE
    # Si hay patrones específicos de "revelar" o "ejecutar código" -> BLOCK directo.
    high_severity_tags = {
        "reveal_system_es",
        "reveal_system_en",
        "code_fence",
        "code_call",
        "html_script",
        "developer_mode",
        "system_tag",
        "fence_tag",
    }
    if any(r in high_severity_tags for r in reasons):
        decision = Decision.BLOCK
    elif "too_long" in reasons:
        decision = Decision.BLOCK
    elif len([r for r in reasons if ":" not in r and r != "too_long"]) >= BLOCK_PATTERN_HITS:
        decision = Decision.BLOCK
    elif len([r for r in reasons if ":" not in r and r != "too_long"]) >= MAX_PATTERN_HITS:
        decision = Decision.SUSPICIOUS
    elif "low_legible_ratio" in reasons or had_invisible:
        decision = Decision.SUSPICIOUS

    return GuardResult(decision, sanitized, reasons, had_invisible)


# Mensaje neutro que se manda al usuario cuando bloqueamos.
BLOCK_RESPONSE_TEXTS: list[str] = [clinic_profile.OUT_OF_SCOPE_TEXT]
