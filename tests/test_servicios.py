from app.clinic_profile import NAME, SERVICES, fill
from app.tools.contactos import merge_notes
from app.tools.servicios import search


def test_busqueda_vacia_devuelve_todo_el_catalogo():
    assert len(search("")) == len(SERVICES)


def test_limpieza_devuelve_limpieza_primero():
    res = search("cuánto cuesta una limpieza")
    assert res[0]["id"] == "limpieza"


def test_sinonimo_calza_devuelve_resinas():
    res = search("se me cayó una calza")
    assert res[0]["id"] == "resinas"


def test_servicio_que_no_existe_no_devuelve_nada():
    assert search("brackets") == []


def test_placeholders_del_perfil_se_rellenan():
    out = fill("Hola desde {{CLINIC_NAME}}. {{CLINIC_URGENCIAS}}")
    assert "{{" not in out
    assert NAME in out


def test_notas_bot_se_crean_y_combinan():
    first = merge_notes(None, {"urgencia": "alta"})
    assert first == "[Bot] urgencia: alta"
    second = merge_notes(first, {"origen": "Instagram"})
    assert second == "[Bot] urgencia: alta · origen: Instagram"


def test_notas_humanas_no_se_pisan():
    assert merge_notes("Paciente prefiere cita por la tarde", {"urgencia": "baja"}) is None
