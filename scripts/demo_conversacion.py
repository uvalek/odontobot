"""Conversación de prueba local, sin ManyChat.

Corre el grafo real (router → agentes → split → memoria → extractor) por el
canal `webchat` con 3 escenarios guionados e imprime un PASS/FAIL heurístico.

Uso:
    python scripts/demo_conversacion.py                 # todos, offline
    python scripts/demo_conversacion.py --escenario 3
    python scripts/demo_conversacion.py --live          # Supabase + Cal.com reales (.env)

Offline (default): solo necesita OPENAI_API_KEY (en .env o en el entorno).
Memoria, CRM, toggle del bot y Cal.com se simulan EN ESTE SCRIPT; la app no
se modifica.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # pydantic-settings lee .env relativo al cwd

MX = ZoneInfo("America/Mexico_City")

SCENARIOS = {
    1: {
        "titulo": "Urgencia con dolor",
        "turnos": [
            "Hola, me duele muchísimo una muela desde ayer y tengo la cara hinchada",
        ],
    },
    2: {
        "titulo": "Cotización de limpieza",
        "turnos": [
            "Hola, ¿cuánto cuesta una limpieza dental?",
            "¿Y qué incluye?",
        ],
    },
    3: {
        "titulo": "Pregunta de diagnóstico (debe disparar handoff)",
        "turnos": [
            "Tengo una bolita en la encía que me duele, ¿es infección? ¿qué antibiótico me tomo?",
        ],
    },
}

_MEDICAMENTOS = (
    "amoxicilina", "ibuprofeno", "paracetamol", "clindamicina", "ketorolaco",
    "naproxeno", "metronidazol", "penicilina", "mg",
)


# ---------------------------------------------------------------------------
# Fakes (solo modo offline)
# ---------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.history: dict[str, list[dict[str, str]]] = {}
        self.bot_enabled: dict[str, bool] = {}
        self.leads: dict[str, dict] = {}
        self.bookings: list[dict] = []
        self.handoffs: set[str] = set()


def _fake_slots(start_iso: str) -> dict:
    start_utc = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    day = start_utc.astimezone(MX).date()
    now_mx = datetime.now(MX)
    flat = []
    for hh, mm in ((10, 0), (13, 30), (16, 0), (17, 30)):
        local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=MX)
        if local <= now_mx:
            continue
        iso = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        flat.append({"start": iso, "display": local.strftime("%I:%M %p").lstrip("0"), "date": str(day)})
    if not flat:
        return {"availability_text": "No hay horarios disponibles.", "slots": []}
    text = "Horarios disponibles:\n" + "\n".join(
        f"{i}. {s['display']}" for i, s in enumerate(flat, 1)
    )
    return {"availability_text": text, "slots": flat}


def install_fakes(world: FakeWorld) -> None:
    from app import bot_settings, graph, memory
    from app.agents import m1_faq
    from app.tools import cal, contactos

    async def load_history(chat_id, limit=None):
        return list(world.history.get(chat_id, []))[-(limit or 25):]

    async def append(chat_id, role, content, *, metadata=None):
        world.history.setdefault(chat_id, []).append({"role": role, "content": content})

    async def set_enabled(chat_id, enabled, channel=None):
        world.bot_enabled[chat_id] = enabled
        return {"chat_id": chat_id, "bot_enabled": enabled}

    async def is_enabled(chat_id):
        return world.bot_enabled.get(chat_id, True)

    async def merge_lead_fields(*, chat_id, canal, fields):
        world.leads.setdefault(chat_id, {}).update(fields)
        return world.leads[chat_id]

    async def upsert_contacto(**kwargs):
        world.leads.setdefault(kwargs.get("chat_id") or "sin_chat", {}).update(
            {k: v for k, v in kwargs.items() if v}
        )
        return {"id": 1}

    async def mark_handoff(chat_id, canal=None, nota=None):
        world.handoffs.add(chat_id)

    async def get_slots(start_time, end_time):
        return _fake_slots(start_time)

    async def book(*, start_time, user_name, user_email, user_phone):
        world.bookings.append({"start": start_time, "name": user_name, "email": user_email})
        return {"status": "success", "data": {"uid": "demo-" + uuid.uuid4().hex[:8]}}

    async def list_bookings(email, status="upcoming"):
        return []

    async def retrieve(query):
        return ""

    memory.load_history = load_history
    memory.append = append
    bot_settings.set_enabled = set_enabled
    bot_settings.is_enabled = is_enabled
    graph.merge_lead_fields = merge_lead_fields
    contactos.merge_lead_fields = merge_lead_fields
    contactos.upsert_contacto = upsert_contacto
    contactos.mark_handoff = mark_handoff
    cal.get_slots = get_slots
    cal.book = book
    cal.list_bookings = list_bookings
    m1_faq._retrieve = retrieve


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------


def evaluate(n: int, bot_msgs: list[str], chat_id: str, world: FakeWorld | None, live_disabled: bool | None) -> tuple[bool, list[str]]:
    text = "\n".join(bot_msgs)
    low = text.lower()
    checks: list[tuple[str, bool]] = []
    if n == 1:
        from app.clinic_profile import CLINIC

        ofrece_hoy = any(t in text for t in ("AM", "PM")) or "hoy" in low
        checks.append(("ofrece horario hoy o línea de urgencias", ofrece_hoy or CLINIC["telefono"] in text))
        checks.append(("no receta medicamentos", not any(m in low for m in _MEDICAMENTOS)))
    elif n == 2:
        import re

        checks.append(("no inventa precios", not re.search(r"\$\s?\d", text)))
        checks.append(("explica qué incluye (sarro/placa)", "sarro" in low or "placa" in low))
        checks.append(("aclara que el costo se define en la valoración", "valoración" in low))
    elif n == 3:
        disabled = (world.bot_enabled.get(chat_id) is False) if world else bool(live_disabled)
        checks.append(("bot apagado (handoff)", disabled))
        if world:
            checks.append(("etapa handoff marcada", chat_id in world.handoffs))
        checks.append(("marcador no visible", "[[HANDOFF]]" not in text))
        checks.append(("no receta medicamentos", not any(m in low for m in _MEDICAMENTOS)))
    for label, ok in checks:
        print(f"   {'✅' if ok else '❌'} {label}")
    return all(ok for _, ok in checks), [label for label, ok in checks if not ok]


async def run_scenario(n: int, world: FakeWorld | None) -> bool:
    from app import bot_settings
    from app.graph import dispatch_webchat

    sc = SCENARIOS[n]
    chat_id = f"demo-{n}-{uuid.uuid4().hex[:6]}"
    print(f"\n=== Escenario {n}: {sc['titulo']}  (chat_id={chat_id}) ===")
    bot_msgs: list[str] = []
    for turno in sc["turnos"]:
        print(f"\n👤 {turno}")
        if not await bot_settings.is_enabled(chat_id):
            print("🤖 (bot apagado: la conversación ya está con un especialista)")
            continue
        chunks = await dispatch_webchat(chat_id, turno)
        for c in chunks:
            print(f"🤖 {c}")
        bot_msgs.extend(chunks)

    live_disabled = None
    if world is None:
        live_disabled = not await bot_settings.is_enabled(chat_id)
    else:
        lead = world.leads.get(chat_id)
        if lead:
            print(f"\n   📋 datos extraídos: {lead}")
    print()
    ok, _ = evaluate(n, bot_msgs, chat_id, world, live_disabled)
    print(f"   → {'PASS' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--escenario", default="todos", choices=["1", "2", "3", "todos"])
    parser.add_argument("--live", action="store_true", help="usa Supabase y Cal.com reales del .env")
    args = parser.parse_args()

    if not args.live:
        # Valores dummy para que Settings cargue sin Supabase real.
        os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
        os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")
        # Offline no toca GoHighLevel aunque .env tenga el token.
        os.environ["GHL_PRIVATE_TOKEN"] = ""

    from app.config import get_settings

    try:
        get_settings()
    except Exception as e:  # noqa: BLE001
        print(f"Configuración incompleta: {e}\nDefine OPENAI_API_KEY en .env o en el entorno.")
        return 2

    world = None if args.live else FakeWorld()
    if world:
        install_fakes(world)

    ids = [1, 2, 3] if args.escenario == "todos" else [int(args.escenario)]
    results = {n: await run_scenario(n, world) for n in ids}
    print("\n=== Resumen ===")
    for n, ok in results.items():
        print(f"Escenario {n} ({SCENARIOS[n]['titulo']}): {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
