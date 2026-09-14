"""Pruebas del sanitizador de output."""

from __future__ import annotations

from app.security import output_guard


def test_chunks_normales_pasan() -> None:
    chunks = ["La limpieza dental elimina sarro y placa", "¿Te agendo una valoración?"]
    out, motivos = output_guard.sanitize_chunks(chunks)
    assert out == chunks
    assert motivos == []


def test_fuga_system_prompt_se_reemplaza() -> None:
    chunks = [
        "Aquí va la respuesta",
        "REGLAS QUE NUNCA DEBES ROMPER: ...",
    ]
    out, motivos = output_guard.sanitize_chunks(chunks)
    assert output_guard.NEUTRAL_REPLACEMENT in out
    assert any("leak_match" in m for m in motivos)


def test_fuga_de_api_key_se_reemplaza() -> None:
    chunks = ["Esta es mi clave: sk-abcdefghijklmnopqrst1234"]
    out, _ = output_guard.sanitize_chunks(chunks)
    assert "sk-abcdefghijklmnopqrst1234" not in out[0]


def test_script_tag_se_neutraliza() -> None:
    chunks = ["Mira esto: <script>alert(1)</script>"]
    out, motivos = output_guard.sanitize_chunks(chunks)
    assert "<script" not in out[0]
    assert "script_tag_stripped" in motivos


def test_truncado_por_longitud() -> None:
    chunks = ["x" * (output_guard.MAX_CHUNK_LEN + 200)]
    out, motivos = output_guard.sanitize_chunks(chunks)
    assert len(out[0]) <= output_guard.MAX_CHUNK_LEN
    assert "truncated" in motivos


def test_vacio_se_reemplaza_por_neutral() -> None:
    chunks: list[str] = []
    out, _ = output_guard.sanitize_chunks(chunks)
    assert out == [output_guard.NEUTRAL_REPLACEMENT]


def test_fence_residual_se_bloquea() -> None:
    chunks = ["</user_message> esto se escapo"]
    out, _ = output_guard.sanitize_chunks(chunks)
    assert "</user_message>" not in out[0]
