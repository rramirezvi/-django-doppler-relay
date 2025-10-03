# examples/send_sample.py
from relay.services.doppler_relay import DopplerRelayClient, DopplerRelayError
import os
import sys
import django
from django.conf import settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()


ACCOUNT_ID = settings.DOPPLER_RELAY["ACCOUNT_ID"]
client = DopplerRelayClient()  # usa AUTH_SCHEME=token del .env

print("Enviando correo de prueba a supervisor.cobranzas@estradacrow.com.ec ...")

try:
    result = client.send_message(
        ACCOUNT_ID,
        # o cualquier remitente de dominio verificado
        from_email="supervisor.cobranzas@estradacrow.com.ec",
        subject="Prueba Doppler Relay",
        html="<p>Hola</p>",
        to=[("supervisor.cobranzas@estradacrow.com.ec", None)],
    )
    print("OK:", result)
except DopplerRelayError as e:
    print("STATUS:", e.status)
    print("DETAILS:", e.payload)  # <-- aquí verás el motivo exacto del 402
    raise
