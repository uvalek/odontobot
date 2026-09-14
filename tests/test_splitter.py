from app.splitter import split_response


def test_array_basico():
    raw = '["hola", "como estas"]'
    assert split_response(raw) == ["hola", "como estas"]


def test_array_con_fence_json():
    raw = '```json\n["uno", "dos"]\n```'
    assert split_response(raw) == ["uno", "dos"]


def test_texto_plano_devuelve_un_solo_chunk():
    raw = "esto no es json"
    assert split_response(raw) == ["esto no es json"]


def test_filtra_strings_vacios():
    raw = '["hola", "", "  ", "fin"]'
    assert split_response(raw) == ["hola", "fin"]


def test_string_dentro_de_array_con_emoji():
    raw = '["¡Hola! 🦷", "¿En qué te ayudo?"]'
    assert split_response(raw) == ["¡Hola! 🦷", "¿En qué te ayudo?"]


def test_vacio():
    assert split_response("") == []


def test_objeto_no_lista_se_vuelve_string():
    raw = '{"foo": "bar"}'
    out = split_response(raw)
    assert len(out) == 1


# --- Casos de recuperación cuando el modelo emite JSON imperfecto ---

def test_recupera_items_sin_corchetes():
    # Modelo a veces devuelve solo "a","b" sin envolver en []
    raw = '"hola", "como estas"'
    assert split_response(raw) == ["hola", "como estas"]


def test_recupera_dos_arrays_pegados():
    # Caso real visto en producción: el modelo produjo dos respuestas
    # seguidas separadas por línea en blanco.
    raw = '"Gracias por la info", "Dime la fecha"\n\n  "Perfecto", "Reviso horarios"'
    out = split_response(raw)
    assert "Gracias por la info" in out
    assert "Reviso horarios" in out


def test_recupera_strings_de_texto_basurita():
    raw = 'algún preludio "uno" comentario "dos" cierre'
    out = split_response(raw)
    assert out == ["uno", "dos"]


def test_dos_arrays_identicos_se_deduplican():
    # El modelo a veces emite su respuesta DOS veces (array repetido,
    # separado por línea en blanco). No debemos enviar el mismo bloque dos veces.
    out = split_response('["a", "b"]\n\n["a", "b"]')
    assert out == ["a", "b"]


def test_array_con_items_repetidos_se_deduplica():
    out = split_response('["hola", "mundo", "hola"]')
    assert out == ["hola", "mundo"]


def test_coalesce_si_pasa_el_limite():
    raw = '["a", "b", "c", "d", "e", "f"]'
    out = split_response(raw)
    assert len(out) == 4  # MAX_MESSAGES
    assert out[-1].count("\n") >= 2  # los últimos juntados con saltos
