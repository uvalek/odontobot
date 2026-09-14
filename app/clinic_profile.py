"""Perfil del negocio (capa de dominio).

TODOS los datos de la clinica viven aqui, separados de los prompts. Para
apuntar el bot a otra clinica basta con editar este archivo: los prompts
reciben estos datos via placeholders `{{CLINIC_*}}` (ver
`app/security/system_prompt.py`) y los textos visibles fijos (bloqueo,
fallbacks, handoff) se derivan de aqui.

Modulo de datos puro: sin imports de la app para evitar ciclos.
"""

from __future__ import annotations

CLINIC: dict = {
    "nombre": "Clínica Dental Aurea",
    "eslogan": "Odontología integral y estética dental",
    "direccion": "Av. Reforma Norte 1204, Col. Centro, Puebla, Pue.",
    "referencia": "A una cuadra del Paseo Bravo, con estacionamiento propio",
    "telefono": "222 123 4567",
    "whatsapp": "222 123 4567",
    "horarios": [
        "Lunes a viernes: 9:00 a 19:00",
        "Sábado: 9:00 a 14:00",
        "Domingo: cerrado",
    ],
    "urgencias": "Línea de urgencias 222 123 4567, disponible hasta las 22:00 entre semana",
    "equipo": [
        {
            "nombre": "Dra. Mariana Estrada",
            "rol": "Directora, odontología general y estética",
            "cedula": "1234567",
        },
        {"nombre": "Dr. Ricardo Peña", "rol": "Ortodoncia y ortopedia maxilar", "cedula": ""},
        {"nombre": "Dra. Sofía Lira", "rol": "Endodoncia", "cedula": ""},
        {"nombre": "Dr. Andrés Cuevas", "rol": "Implantología y cirugía oral", "cedula": ""},
    ],
    "formas_pago": [
        "Efectivo",
        "Tarjeta de crédito o débito",
        "Transferencia",
        "Meses sin intereses en compras desde $3,000",
        "Plan de pagos interno para ortodoncia e implantes (enganche + mensualidades)",
    ],
    "aseguradoras": (
        "No se factura directo a la aseguradora, pero se entrega factura y "
        "expediente para que el paciente solicite su reembolso."
    ),
    "politicas": [
        "Las citas se agendan con al menos 24 horas de anticipación",
        "Las cancelaciones se avisan con al menos 4 horas de anticipación",
        "Tolerancia de 15 minutos",
        "La primera cita dura aproximadamente 40 minutos",
    ],
}

# Catalogo de servicios. `precio` es el precio de referencia "desde" (None =
# solo cotizacion tras valoracion). `alias` alimenta la busqueda de M3.
SERVICES: list[dict] = [
    {
        "id": "valoracion",
        "nombre": "Consulta y valoración",
        "categoria": "limpieza_revision",
        "precio": 300,
        "texto_precio": "$300",
        "incluye": "Revisión completa y plan de tratamiento. Se bonifica si el tratamiento se realiza el mismo día.",
        "alias": ["consulta", "valoracion", "revision", "chequeo", "primera cita", "diagnostico"],
    },
    {
        "id": "limpieza",
        "nombre": "Limpieza dental (profilaxis)",
        "categoria": "limpieza_revision",
        "precio": 800,
        "texto_precio": "desde $800",
        "incluye": "Retiro de sarro y placa, pulido dental.",
        "alias": ["limpieza", "profilaxis", "sarro", "placa"],
    },
    {
        "id": "resina",
        "nombre": "Resina / empaste",
        "categoria": "limpieza_revision",
        "precio": 900,
        "texto_precio": "desde $900",
        "incluye": "Restauración de una pieza con resina del color del diente.",
        "alias": ["resina", "empaste", "caries", "calza", "tapadura", "obturacion"],
    },
    {
        "id": "extraccion",
        "nombre": "Extracción simple",
        "categoria": "dolor_urgencia",
        "precio": 1200,
        "texto_precio": "desde $1,200",
        "incluye": "Extracción de una pieza sin complicaciones quirúrgicas.",
        "alias": ["extraccion", "sacar muela", "sacar diente", "quitar muela"],
    },
    {
        "id": "muela_juicio",
        "nombre": "Extracción de muela del juicio",
        "categoria": "dolor_urgencia",
        "precio": 3500,
        "texto_precio": "desde $3,500",
        "incluye": "Procedimiento con el especialista en cirugía oral.",
        "alias": ["muela del juicio", "cordal", "tercer molar", "juicio"],
    },
    {
        "id": "endodoncia",
        "nombre": "Endodoncia",
        "categoria": "dolor_urgencia",
        "precio": 3800,
        "texto_precio": "desde $3,800",
        "incluye": "Tratamiento de conductos realizado por la especialista en endodoncia.",
        "alias": ["endodoncia", "conductos", "matar nervio", "nervio"],
    },
    {
        "id": "corona",
        "nombre": "Corona de zirconia",
        "categoria": "implantes_protesis",
        "precio": 7500,
        "texto_precio": "desde $7,500",
        "incluye": "Corona estética de zirconia por pieza.",
        "alias": ["corona", "zirconia", "funda", "protesis"],
    },
    {
        "id": "blanqueamiento",
        "nombre": "Blanqueamiento en consultorio",
        "categoria": "estetica_blanqueamiento",
        "precio": 4500,
        "texto_precio": "desde $4,500",
        "incluye": "Sesión de blanqueamiento profesional en consultorio.",
        "alias": ["blanqueamiento", "blanquear", "dientes blancos", "aclarar"],
    },
    {
        "id": "brackets",
        "nombre": "Ortodoncia con brackets metálicos",
        "categoria": "ortodoncia",
        "precio": 12000,
        "texto_precio": "$12,000",
        "incluye": "Colocación de brackets y 12 meses de citas de control.",
        "alias": ["ortodoncia", "brackets", "frenos", "fierros", "enderezar dientes"],
    },
    {
        "id": "alineadores",
        "nombre": "Alineadores transparentes",
        "categoria": "ortodoncia",
        "precio": 35000,
        "texto_precio": "desde $35,000",
        "incluye": "Ortodoncia invisible con alineadores removibles.",
        "alias": ["alineadores", "invisalign", "ortodoncia invisible", "transparentes"],
    },
    {
        "id": "implante",
        "nombre": "Implante dental unitario",
        "categoria": "implantes_protesis",
        "precio": 18000,
        "texto_precio": "desde $18,000",
        "incluye": "Implante de una pieza con el especialista en implantología.",
        "alias": ["implante", "implantes", "diente perdido", "tornillo"],
    },
    {
        "id": "diseno_sonrisa",
        "nombre": "Diseño de sonrisa",
        "categoria": "estetica_blanqueamiento",
        "precio": None,
        "texto_precio": "cotización tras valoración",
        "incluye": "Plan estético personalizado (carillas, resinas, blanqueamiento según el caso).",
        "alias": ["diseño de sonrisa", "diseno de sonrisa", "carillas", "sonrisa", "estetica"],
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
    f"Si el dolor es muy fuerte o empeora, puedes llamar a nuestra línea de "
    f"urgencias al {CLINIC['telefono']} (entre semana hasta las 22:00)."
)

HANDOFF_MESSAGE: str = (
    "Esa pregunta la debe responder uno de nuestros especialistas. "
    "Ya le avisé al equipo y en breve te atienden por este mismo chat."
)

OUT_OF_SCOPE_TEXT: str = (
    f"Solo puedo ayudarte con temas de {NAME}: servicios, precios de "
    f"referencia, horarios y citas. ¿En qué de eso te puedo ayudar?"
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
        lines.append(f"- {s['nombre']}: {s['texto_precio']}. {s['incluye']}")
    return "\n".join(lines)


def render_profile() -> str:
    c = CLINIC
    horarios = "\n".join(f"- {h}" for h in c["horarios"])
    pagos = "\n".join(f"- {p}" for p in c["formas_pago"])
    politicas = "\n".join(f"- {p}" for p in c["politicas"])
    return (
        f"Nombre: {c['nombre']}\n"
        f"Eslogan: {c['eslogan']}\n"
        f"Dirección: {c['direccion']}\n"
        f"Referencia: {c['referencia']}\n"
        f"Teléfono / WhatsApp: {c['telefono']}\n"
        f"Horarios:\n{horarios}\n"
        f"Urgencias: {c['urgencias']}\n"
        f"Equipo:\n{render_team()}\n"
        f"Servicios y precios de referencia (siempre como precio desde, sujeto a valoración):\n"
        f"{render_services()}\n"
        f"Formas de pago:\n{pagos}\n"
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
        "{{URGENCY_LINE_TEXT}}": URGENCY_LINE_TEXT,
        "{{HANDOFF_MESSAGE}}": HANDOFF_MESSAGE,
    }


def fill(text: str) -> str:
    for k, v in placeholders().items():
        text = text.replace(k, v)
    return text
