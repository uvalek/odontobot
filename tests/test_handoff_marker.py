import os

# app.graph carga los prompts al importar y eso instancia Settings.
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")

from app import graph  # noqa: E402


async def test_split_quita_marcador_y_marca_handoff():
    state = {
        "chat_id": "c1",
        "channel": "webchat",
        "agent_response": '["Un especialista te atiende en breve. [[HANDOFF]]"]',
    }
    out = await graph._split(state)
    assert out["handoff"] is True
    assert out["chunks"] == ["Un especialista te atiende en breve."]
    assert graph.HANDOFF_MARKER not in out["agent_response"]


async def test_split_sin_marcador_no_hace_handoff():
    out = await graph._split({"agent_response": '["Hola", "¿En qué te ayudo?"]'})
    assert out["handoff"] is False
    assert out["chunks"] == ["Hola", "¿En qué te ayudo?"]


async def test_save_memory_con_handoff_apaga_el_bot(monkeypatch):
    calls: dict = {}

    async def fake_append(*a, **k):
        return None

    async def fake_set_enabled(chat_id, enabled, channel=None):
        calls["toggle"] = (chat_id, enabled, channel)

    async def fake_mark_handoff(chat_id, canal=None, nota=None):
        calls["etapa"] = chat_id

    monkeypatch.setattr(graph.memory, "append", fake_append)
    monkeypatch.setattr(graph.bot_settings, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(graph.contactos, "mark_handoff", fake_mark_handoff)

    await graph._save_memory(
        {
            "chat_id": "c1",
            "channel": "manychat",
            "subchannel": "whatsapp",
            "user_text_raw": "¿qué antibiótico tomo?",
            "chunks": ["Un especialista te atiende en breve."],
            "handoff": True,
        }
    )
    assert calls["toggle"] == ("c1", False, "manychat")
    assert calls["etapa"] == "c1"
