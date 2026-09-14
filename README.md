# Odontobot — demo de recepcionista virtual para clínicas dentales

Chatbot de atención y calificación de pacientes (Python + FastAPI + LangGraph + Supabase) para WhatsApp, Instagram y Messenger (vía ManyChat), Telegram y chat web. Clínica configurada: **Swiss Dental — Dr. Arturo Ramirez** (San Diego Metepec, Tlaxcala), con los datos de su landing. Precios y formas de pago están pendientes de confirmar.

```
Webhook → buffer → resolver media → memoria → router → M1|M2|M3|M4 → split → enviar → guardar memoria → extraer datos del paciente
```

## Cambiar de clínica: un solo archivo

Todos los datos del negocio viven en **`app/clinic_profile.py`**: nombre, dirección, horarios, línea de urgencias, equipo, servicios y precios de referencia, formas de pago, aseguradoras, políticas y los textos fijos (handoff, fuera de tema, fallbacks). Los prompts los reciben mediante placeholders `{{CLINIC_*}}` que rellena `app/security/system_prompt.py`.

## Agentes

| Módulo | Archivo | Qué hace |
|---|---|---|
| Router | `app/agents/router.py` + `app/prompts/router.md` | Clasifica el mensaje en M1/M2/M3/M4 |
| M1 Información | `app/agents/m1_faq.py` + `m1_faq.md` | Horarios, ubicación, doctores, pagos, aseguradoras, políticas |
| M2 Agendamiento | `app/agents/m2_agendamiento.py` + `m2_agendamiento.md` | Urgencias, GATE 1 (motivo, urgencia, nuevo/seguimiento, disponibilidad), cita en Cal.com, GATE 2 (edad/tutor, origen, forma de pago) |
| M3 Servicios | `app/agents/m3_catalogo.py` + `m3_catalogo.md` | Qué incluye cada servicio y cómo se define el costo, con la tool `buscar_servicios` |
| M4 Seguimiento | `app/agents/m4_seguimiento.py` + `m4_seguimiento.md` | Pacientes que regresan y casos para especialista |
| Extractor | `app/agents/extractor.py` | Guarda los datos del paciente en `contactos` |

### Handoff (GATE 3)

Ante preguntas de diagnóstico, medicamentos o dosis, precio cerrado de un caso, quejas o seguimiento de un tratamiento en curso, el agente avisa que un especialista lo atiende y agrega el marcador interno `[[HANDOFF]]`. El grafo (`app/graph.py`) quita el marcador, apaga el bot para esa conversación (`bot_settings.bot_enabled = false`, el mismo toggle del dashboard) y marca la etapa `handoff`. Para reactivar el bot: dashboard (`PATCH /api/conversations/{chat_id}` con `bot_enabled: true`).

### Campos del paciente (demo: columnas reutilizadas)

La traducción vive en `app/tools/contactos.py::LEAD_COLUMNS`:

| Dato | Columna de `contactos` |
|---|---|
| Motivo de consulta | `zona_interes` |
| Edad | `presupuesto_max` |
| Forma de pago | `tipo_credito` |
| Fecha de la cita | `fecha_visita` |
| Urgencia, nuevo/seguimiento, disponibilidad, tutor, cómo se enteró | `notas_internas` (línea `[Bot] …`) |

## Setup local

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env
# completa OPENAI_API_KEY, SUPABASE_*, CAL_API_KEY, CAL_EVENT_TYPE_ID, tokens de canales

# Esquema de Supabase (SQL Editor o CLI), en orden:
#   supabase/migrations/001_message_buffer.sql
#   supabase/migrations/002_channel_flags.sql
#   supabase/migrations/003_esquema_demo_clinica.sql

uvicorn app.main:app --reload
```

Tests:

```bash
pytest -q
```

## Probar una conversación sin ManyChat

```bash
python scripts/demo_conversacion.py --escenario todos
```

Corre 3 escenarios contra el grafo real (canal webchat): urgencia con dolor, cotización de limpieza y pregunta de diagnóstico (debe disparar handoff). Por defecto solo necesita `OPENAI_API_KEY`: memoria, CRM y Cal.com se simulan en memoria. Con `--live` usa Supabase y Cal.com reales del `.env`.

## Conectar canales

### Telegram

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://TU_DOMINIO/webhook/telegram&secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

### ManyChat (WhatsApp / Instagram / Messenger)

En el flujo de ManyChat agrega una *External Request* `POST` a `https://TU_DOMINIO/webhook/manychat` con el JSON del subscriber (incluye `id`, `text`, `last_interaction.url` y `last_interaction.mime_type` cuando aplique).

### Chat web

`POST /api/webchat` con `{"chat_id": "...", "text": "..."}` (header `X-API-Key` si `WEBCHAT_API_KEY` está definido).

## Panel de canales

`/panel` enciende o apaga el bot por canal (token `TEST_ARM_TOKEN`).
