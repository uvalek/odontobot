import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")

from app.tools import ghl  # noqa: E402


def test_free_slots_se_normalizan_a_utc_y_hora_mx():
    body = {
        "2026-09-15": {"slots": ["2026-09-15T10:00:00-06:00", "2026-09-15T16:30:00-06:00"]},
        "2026-09-16": {"slots": []},
        "traceId": "abc",
    }
    out = ghl.normalize_free_slots(body)
    assert out["slots"][0] == {"start": "2026-09-15T16:00:00Z", "display": "10:00 AM", "date": "2026-09-15"}
    assert out["slots"][1]["display"] == "4:30 PM"
    assert "martes 15 de septiembre de 2026" in out["availability_text"]


def test_sin_slots():
    assert ghl.normalize_free_slots({"traceId": "x"})["slots"] == []


def test_campos_personalizados_usan_etiquetas_y_ids():
    ids = {"motivo_consulta": "f1", "nivel_urgencia": "f2", "edad": "f3", "tutor": "f4"}
    out = ghl.build_custom_fields(
        {"motivo_consulta": "limpieza_revision", "nivel_urgencia": "alta", "edad": "34",
         "tutor": "", "forma_pago": "msi"},
        ids,
    )
    assert {"id": "f1", "field_value": "Limpieza o revisión"} in out
    assert {"id": "f2", "field_value": "Alta"} in out
    assert {"id": "f3", "field_value": 34} in out
    assert all(f["id"] != "f4" for f in out)  # vacío no se manda
    assert len(out) == 3  # forma_pago no tiene id -> se omite


def test_error_de_duplicado_expone_el_campo():
    err = ghl.GHLError(
        "dup",
        status=400,
        data={"message": "This location does not allow duplicated contacts.", "meta": {"matchingField": "phone"}},
    )
    assert err.duplicate_field == "phone"
    assert ghl.GHLError("otro", status=400, data={"message": "x"}).duplicate_field is None


async def test_actualizar_omite_telefono_duplicado(monkeypatch):
    from app.tools import ghl_sync

    calls = []

    async def fake_update(contact_id, **fields):
        calls.append(fields)
        if fields.get("telefono"):
            raise ghl.GHLError(
                "dup", status=400,
                data={"message": "This location does not allow duplicated contacts.",
                      "meta": {"matchingField": "phone", "contactName": "Alek"}},
            )

    monkeypatch.setattr(ghl, "update_contact", fake_update)
    notes = await ghl_sync._update_skipping_duplicates("c1", nombre="Adam", correo="a@b.com", telefono="2411363909")
    assert calls[-1]["telefono"] is None and calls[-1]["correo"] == "a@b.com"
    assert "Alek" in notes[0]


def test_telefono_se_normaliza_a_e164():
    assert ghl._contact_payload(telefono="2411363909")["phone"] == "+522411363909"


def test_contacto_borrado_en_ghl_se_detecta():
    err = ghl.GHLError("x", status=400, data={"message": "Contact not found for id:abc"})
    assert err.contact_not_found
    assert not ghl.GHLError("x", status=400, data={"message": "otro"}).contact_not_found


async def test_sync_recrea_contacto_si_fue_borrado(monkeypatch):
    from app.tools import ghl_sync

    saved = {}

    async def fake_row(chat_id):
        return {"id": 7, "chat_id": chat_id, "canal": "webchat", "ghl_contact_id": "viejo",
                "nombre": "Adam", "correo": "a@b.com"}

    async def fake_update(contact_id, **kw):
        raise ghl.GHLError("nf", status=400, data={"message": "Contact not found for id:viejo"})

    async def fake_upsert(**kw):
        return {"id": "nuevo"}

    async def fake_save(row_id, chat_id, canal, contact_id):
        saved["id"] = contact_id

    async def noop(*a, **k):
        return None

    async def no_fields():
        return {}

    monkeypatch.setattr(ghl, "enabled", lambda: True)
    monkeypatch.setattr(ghl_sync, "_row", fake_row)
    monkeypatch.setattr(ghl, "update_contact", fake_update)
    monkeypatch.setattr(ghl, "upsert_contact", fake_upsert)
    monkeypatch.setattr(ghl, "custom_field_ids", no_fields)
    monkeypatch.setattr(ghl, "add_tags", noop)
    monkeypatch.setattr(ghl, "add_note", noop)
    monkeypatch.setattr(ghl_sync, "_save_ghl_id", fake_save)

    cid = await ghl_sync.sync_contact(chat_id="web-1", values={"telefono": "2411249120"}, force=True)
    assert cid == "nuevo" and saved["id"] == "nuevo"
