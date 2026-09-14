import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")

from app.agents.m2_agendamiento import _CONFIRM_RE, missing_booking_data  # noqa: E402

CONVERSACION = [
    "Hola, quiero agendar una limpieza",
    "Me llamo Laura Méndez",
    "laura.mendez@gmail.com",
    "mi cel es 246 144 9999",
]


def test_datos_escritos_por_el_paciente_permiten_agendar():
    args = {"userName": "Laura Méndez", "userEmail": "laura.mendez@gmail.com", "userPhone": "2461449999"}
    assert missing_booking_data(args, user_texts=CONVERSACION, canal="webchat", user_phone="") == []


def test_correo_inventado_bloquea():
    args = {"userName": "Laura Méndez", "userEmail": "laura@example.com", "userPhone": "2461449999"}
    assert missing_booking_data(args, user_texts=CONVERSACION, canal="webchat", user_phone="") == ["correo electrónico"]


def test_sin_nombre_ni_telefono_bloquea():
    args = {"userName": "", "userEmail": "laura.mendez@gmail.com"}
    faltan = missing_booking_data(args, user_texts=CONVERSACION[:3], canal="webchat", user_phone="")
    assert faltan == ["nombre completo del paciente", "número de celular a 10 dígitos"]


def test_whatsapp_no_pide_telefono():
    args = {"userName": "Laura Méndez", "userEmail": "laura.mendez@gmail.com"}
    assert missing_booking_data(args, user_texts=CONVERSACION[:3], canal="whatsapp", user_phone="+522461449999") == []


def test_detecta_confirmacion_falsa():
    assert _CONFIRM_RE.search("Listo, tu cita quedó para el martes a las 10:00 AM")
    assert not _CONFIRM_RE.search("¿Qué horario te acomoda para tu cita?")
