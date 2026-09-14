"""Perfil del negocio (capa de dominio).

TODOS los datos de la clinica viven aqui, separados de los prompts. Para
apuntar el bot a otra clinica basta con editar este archivo: los prompts
reciben estos datos via placeholders `{{CLINIC_*}}` (ver
`app/security/system_prompt.py`) y los textos visibles fijos (bloqueo,
fallbacks, handoff) se derivan de aqui.

Datos actuales: Swiss Dental (Dr. Arturo Ramirez), tomados de la landing
https://landingdrarturo.vercel.app. Lo marcado "POR CONFIRMAR" aun no lo ha
confirmado el consultorio; el bot no inventa esos datos.

Modulo de datos puro: sin imports de la app para evitar ciclos.
"""

from __future__ import annotations

CLINIC: dict = {
    "nombre": "Swiss Dental",
    "eslogan": "Consultorio dental del Dr. Arturo Ramirez. Atención dental para niños y adultos",
    "direccion": "Carretera #31, La Loma Xicohtencatl, 90110 San Diego Metepec, Tlax.",
    "referencia": "",
    "telefono": "246 144 1431",
    "whatsapp": "246 144 1431",
    "horarios": [
        "Lunes a viernes: 10:00 a 14:00 y 16:00 a 19:45",
        "Sábado: 10:00 a 13:45",
        "Domingo: cerrado",
        "En días festivos el horario puede variar",
    ],
    "urgencias": (
        "No hay línea de urgencias aparte. Ante dolor o urgencia, escribir por WhatsApp o "
        "llamar al 246 144 1431 lo antes posible en horario de atención para recibir el "
        "horario disponible más próximo"
    ),
    "equipo": [
        {"nombre": "Dr. Arturo Ramirez", "rol": "Cirujano dentista", "cedula": ""},
    ],
    # POR CONFIRMAR: el consultorio no ha confirmado precios ni formas de pago.
    "precio_consulta": "",
    "formas_pago": [],
    "nota_pagos": (
        "Las formas de pago aún no están confirmadas por chat. El consultorio ofrece "
        "flexibilidad en los tratamientos y explica el costo antes de iniciar; los detalles "
        "de pago se confirman directamente con el consultorio."
    ),
    "aseguradoras": "Por confirmar con el consultorio.",
    "politicas": [
        "Antes de iniciar cualquier tratamiento, el doctor revisa el caso, explica el procedimiento y el costo",
        "Se respeta el horario de la cita (citas a tiempo)",
        "Se atiende a niños y adultos; los menores vienen acompañados de su madre, padre o tutor",
    ],
}

# Catalogo de servicios. `precio` = precio de referencia "desde" (None = el
# costo se informa en la valoracion). `alias` alimenta la busqueda de M3.
SERVICES: list[dict] = [
    {
        "id": "consulta",
        "nombre": "Consulta y revisión general",
        "categoria": "limpieza_revision",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Revisión completa de dientes y encías para detectar a tiempo caries u otros problemas, con un plan de tratamiento explicado paso a paso.",
        "alias": ["consulta", "valoracion", "revision", "chequeo", "primera cita", "revisión general"],
    },
    {
        "id": "limpieza",
        "nombre": "Limpieza dental",
        "categoria": "limpieza_revision",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Eliminación de sarro y placa bacteriana para mantener encías sanas, prevenir caries y lucir una sonrisa más limpia.",
        "alias": ["limpieza", "profilaxis", "sarro", "placa"],
    },
    {
        "id": "resinas",
        "nombre": "Resinas y restauraciones",
        "categoria": "limpieza_revision",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Tratamiento de caries y reparación de dientes fracturados con resinas del color natural del diente.",
        "alias": ["resina", "resinas", "empaste", "caries", "calza", "tapadura", "restauracion", "diente roto", "fracturado"],
    },
    {
        "id": "infantil",
        "nombre": "Odontología infantil",
        "categoria": "odontopediatria",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Atención paciente y amable para los más pequeños, para que su visita al dentista sea tranquila desde la primera cita.",
        "alias": ["niño", "nino", "niña", "nina", "hijo", "hija", "infantil", "odontopediatria", "bebe", "pequeño"],
    },
    {
        "id": "extracciones",
        "nombre": "Extracciones",
        "categoria": "dolor_urgencia",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Extracción de piezas dentales con anestesia local, indicaciones claras de cuidado y seguimiento de tu recuperación.",
        "alias": ["extraccion", "extracciones", "sacar muela", "sacar diente", "quitar muela", "muela del juicio"],
    },
    {
        "id": "estetica",
        "nombre": "Estética dental",
        "categoria": "estetica_blanqueamiento",
        "precio": None,
        "texto_precio": "costo según valoración",
        "incluye": "Tratamientos para mejorar el color y la forma de tus dientes, como blanqueamiento dental, siempre con una valoración previa.",
        "alias": ["estetica", "blanqueamiento", "blanquear", "dientes blancos", "sonrisa", "carillas"],
    },
]

# Categorias de motivo de consulta (se guardan en el CRM; ver LEAD_COLUMNS
# en app/tools/contactos.py).
MOTIVOS: dict[str, str] = {
    "dolor_urgencia": "Dolor o urgencia",
    "limpieza_revision": "Limpieza o revisión",
    "estetica_blanqueamiento": "Estética o blanqueamiento",
    "ortodoncia": "Ortodoncia",
    "implantes_protesis": "Implantes o prótesis",
    "odontopediatria": "Revisión de niño",
}

# ---------------------------------------------------------------------------
# Textos visibles fijos (derivados del perfil)
# ---------------------------------------------------------------------------

NAME: str = CLINIC["nombre"]

URGENCY_LINE_TEXT: str = (
    f"Si el dolor es muy fuerte o empeora, llámanos o escríbenos por WhatsApp al "
    f"{CLINIC['telefono']} lo antes posible para darte el horario más próximo."
)

HANDOFF_MESSAGE: str = (
    "Esa pregunta la debe responder el doctor. "
    "Ya le avisé al equipo y en breve te atienden por este mismo chat."
)

# Cómo hablar de precios cuando no hay cifra confirmada.
PRICE_NOTE: str = (
    f"El costo exacto se define en la valoración: el doctor revisa tu caso y te informa "
    f"el costo antes de iniciar cualquier tratamiento."
    + (f" La consulta cuesta {CLINIC['precio_consulta']}." if CLINIC["precio_consulta"] else "")
)

OUT_OF_SCOPE_TEXT: str = (
    f"Solo puedo ayudarte con temas de {NAME}: servicios, horarios, ubicación y citas. "
    f"¿En qué de eso te puedo ayudar?"
)

TECH_FALLBACK_TEXT: str = (
    "Disculpa, tuve un problema técnico procesando tu mensaje. "
    "¿Me lo puedes repetir, por favor?"
)

BUDGET_EXHAUSTED_TEXT: str = (
    f"Recibí muchos mensajes tuyos hoy. Vuelve mañana o llámanos al {CLINIC['telefono']}."
)


# ---------------------------------------------------------------------------
# Render para prompts
# ---------------------------------------------------------------------------


def render_team() -> str:
    lines = []
    for m in CLINIC["equipo"]:
        ced = f" (Céd. Prof. {m['cedula']})" if m.get("cedula") else ""
        lines.append(f"- {m['nombre']}: {m['rol']}{ced}")
    return "\n".join(lines)


def render_services() -> str:
    lines = []
    for s in SERVICES:
        lines.append(f"- {s['nombre']} ({s['texto_precio']}): {s['incluye']}")
    return "\n".join(lines)


def render_payments() -> str:
    if CLINIC["formas_pago"]:
        return "\n".join(f"- {p}" for p in CLINIC["formas_pago"])
    return CLINIC["nota_pagos"]


def render_profile() -> str:
    c = CLINIC
    horarios = "\n".join(f"- {h}" for h in c["horarios"])
    politicas = "\n".join(f"- {p}" for p in c["politicas"])
    referencia = f"Referencia: {c['referencia']}\n" if c.get("referencia") else ""
    return (
        f"Nombre: {c['nombre']}\n"
        f"Descripción: {c['eslogan']}\n"
        f"Dirección: {c['direccion']}\n"
        f"{referencia}"
        f"Teléfono / WhatsApp: {c['telefono']}\n"
        f"Horarios:\n{horarios}\n"
        f"Urgencias: {c['urgencias']}\n"
        f"Equipo:\n{render_team()}\n"
        f"Servicios (solo estos):\n{render_services()}\n"
        f"Precios: {PRICE_NOTE}\n"
        f"Formas de pago:\n{render_payments()}\n"
        f"Aseguradoras: {c['aseguradoras']}\n"
        f"Políticas:\n{politicas}"
    )


def placeholders() -> dict[str, str]:
    """Mapa placeholder -> valor que se aplica a todos los prompts."""
    return {
        "{{CLINIC_NAME}}": NAME,
        "{{CLINIC_PROFILE}}": render_profile(),
        "{{CLINIC_SERVICES}}": render_services(),
        "{{CLINIC_TEAM}}": render_team(),
        "{{CLINIC_PHONE}}": CLINIC["telefono"],
        "{{CLINIC_URGENCIAS}}": CLINIC["urgencias"],
        "{{CLINIC_PAYMENTS}}": render_payments(),
        "{{PRICE_NOTE}}": PRICE_NOTE,
        "{{URGENCY_LINE_TEXT}}": URGENCY_LINE_TEXT,
        "{{HANDOFF_MESSAGE}}": HANDOFF_MESSAGE,
    }


def fill(text: str) -> str:
    for k, v in placeholders().items():
        text = text.replace(k, v)
    return text
