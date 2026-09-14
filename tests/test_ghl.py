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
