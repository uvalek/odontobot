from app.clinic_profile import SERVICES, fill
from app.tools.contactos import merge_notes
from app.tools.servicios import search


def test_busqueda_vacia_devuelve_todo_el_catalogo():
    assert len(search("")) == len(SERVICES)


def test_brackets_devuelve_ortodoncia_primero():
    res = search("cuánto cuestan los brackets")
    assert res[0]["id"] == "brackets"
    assert res[0]["texto_precio"] == "$12,000"


def test_sinonimo_frenos_invisibles():
    res = search("frenos transparentes")
    assert res[0]["id"] == "alineadores"


def test_sin_resultados():
    assert search("hipoteca") == []


def test_placeholders_del_perfil_se_rellenan():
    out = fill("Hola desde {{CLINIC_NAME}}. {{CLINIC_URGENCIAS}}")
    assert "{{" not in out
    assert "Clínica Dental Aurea" in out


def test_notas_bot_se_crean_y_combinan():
    first = merge_notes(None, {"urgencia": "alta"})
    assert first == "[Bot] urgencia: alta"
    second = merge_notes(first, {"origen": "Instagram"})
    assert second == "[Bot] urgencia: alta · origen: Instagram"


def test_notas_humanas_no_se_pisan():
    assert merge_notes("Paciente prefiere a la Dra. Lira", {"urgencia": "baja"}) is None
