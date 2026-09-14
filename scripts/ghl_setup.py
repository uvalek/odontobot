"""Verifica el acceso a GoHighLevel y crea los campos personalizados del bot.

Uso (lee GHL_PRIVATE_TOKEN, GHL_LOCATION_ID y GHL_CALENDAR_ID de .env):
    python scripts/ghl_setup.py            # revisa permisos y crea campos faltantes
    python scripts/ghl_setup.py --check    # solo revisa, no crea nada

Es idempotente: si un campo ya existe (mismo nombre), no lo duplica.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("OPENAI_API_KEY", "setup")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "setup")

from app.config import get_settings  # noqa: E402
from app.tools import ghl  # noqa: E402


async def check(name: str, coro) -> bool:
    try:
        await coro
        print(f"  ✅ {name}")
        return True
    except ghl.GHLError as e:
        msg = str(e)
        reason = "falta permiso" if " 401" in msg else msg.split(":", 1)[-1][:120]
        print(f"  ❌ {name}: {reason}")
        return False


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    s = get_settings()
    if not ghl.enabled():
        print("Falta GHL_PRIVATE_TOKEN o GHL_LOCATION_ID en .env")
        return 2

    print("Permisos del token:")
    ok_contacts = await check("Contactos (leer)", ghl._request("GET", "/contacts/", params={"locationId": s.ghl_location_id, "limit": 1}))
    ok_cal = await check("Calendario (leer)", ghl._calendar()) if s.ghl_calendar_id else False
    ok_fields = await check("Campos personalizados (leer)", ghl.list_custom_fields())

    if not ok_fields:
        print("\nAgrega a la Private Integration: Custom Fields (View y Edit). Luego vuelve a correr este script.")
        return 1
    if args.check:
        return 0 if (ok_contacts and ok_cal) else 1

    existing = {f.get("name"): f for f in await ghl.list_custom_fields()}
    print("\nCampos personalizados:")
    for key, d in ghl.CUSTOM_FIELDS.items():
        if d["name"] in existing:
            print(f"  = {d['name']} (ya existe)")
            continue
        payload = {"name": d["name"], "dataType": d["dataType"], "model": "contact"}
        opts = ghl.field_options(key)
        if opts:
            payload["options"] = list(opts.values())
        try:
            await ghl._request("POST", f"/locations/{s.ghl_location_id}/customFields", json=payload)
            print(f"  + {d['name']} (creado)")
        except ghl.GHLError as e:
            print(f"  ❌ {d['name']}: {str(e)[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
