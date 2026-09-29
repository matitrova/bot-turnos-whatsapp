"""Prueba el webhook del puente sin Kapso, sin agente y sin Google Calendar.

Manda webhooks armados a mano y revisa qué línea le llegaría al agente. El
agente se reemplaza por una función que anota las líneas, y la descarga de
audios de Kapso por una de mentira; la transcripción con Whisper es la real
(el audio de prueba lo genera el comando `say` de la Mac).
"""
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import time

os.environ.setdefault("KAPSO_API_KEY", "clave-de-prueba")
os.environ.setdefault("KAPSO_WEBHOOK_SECRET", "secreto-de-prueba")
import puente  # noqa: E402

lineas = []
puente.hablar_con_agente = lambda telefono, linea: lineas.append(linea)

AUDIO_DE_PRUEBA = os.path.join(tempfile.mkdtemp(), "audio.aiff")
subprocess.run(["say", "-v", "Eddy (Español (México))", "-o", AUDIO_DE_PRUEBA,
                "Dale, traela el sábado a las diez"], check=True)


class RespuestaFalsa:
    def __init__(self, datos=None, contenido=b""):
        self.datos, self.content = datos, contenido

    def raise_for_status(self):
        pass

    def json(self):
        return self.datos


def get_falso(url, **kwargs):
    if "media_download" in url:
        with open(AUDIO_DE_PRUEBA, "rb") as archivo:
            return RespuestaFalsa(contenido=archivo.read())
    return RespuestaFalsa({"download_url": "https://api.kapso.ai/meta/whatsapp/media_download?token=x"})


puente.requests.get = get_falso
web = puente.app.test_client()
ahora = str(int(time.time()))


def mensaje(id, direccion, tipo="text", texto="hola", origen="cloud_api", timestamp=None, **extra):
    datos = {
        "message": {"id": id, "timestamp": timestamp or ahora, "type": tipo,
                    "kapso": {"direction": direccion, "origin": origen}},
        "conversation": {"phone_number": "5492660000000", "contact_name": "Cliente de prueba"},
        "phone_number_id": "123",
    }
    if tipo == "text":
        datos["message"]["text"] = {"body": texto}
    datos["message"].update(extra)
    return datos


def enviar(datos, evento="whatsapp.message.received", firma=True):
    cuerpo = json.dumps(datos).encode()
    encabezados = {"X-Webhook-Event": evento, "Content-Type": "application/json"}
    if firma:
        encabezados["X-Webhook-Signature"] = hmac.new(
            puente.KAPSO_WEBHOOK_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    respuesta = web.post("/webhook", data=cuerpo, headers=encabezados)
    puente.cola.join()
    return respuesta.status_code


fallas = 0


def revisar(nombre, condicion, detalle=""):
    global fallas
    print(("OK   " if condicion else "FALLA"), nombre, detalle if not condicion else "")
    fallas += 0 if condicion else 1


# Seguridad
revisar("sin firma -> 401", enviar(mensaje("m1", "inbound"), firma=False) == 401)
cuerpo_adulterado = mensaje("m2", "inbound")
r = web.post("/webhook", data=json.dumps(cuerpo_adulterado),
             headers={"X-Webhook-Event": "whatsapp.message.received", "X-Webhook-Signature": "0" * 64,
                      "Content-Type": "application/json"})
revisar("firma equivocada -> 401", r.status_code == 401)
revisar("nada de eso llegó al agente", lineas == [], lineas)

# Texto del cliente y del lavadero (lo que Facundo manda desde su celular)
enviar(mensaje("m3", "inbound", texto="Hola Facu, tenés turno mañana?"))
revisar("cliente", lineas[-1].endswith("CLIENTE (5492660000000, Cliente de prueba): Hola Facu, tenés turno mañana?"),
        lineas[-1:])
enviar(mensaje("m4", "outbound", texto="Sisi a qué hora", origen="business_app"), evento="whatsapp.message.sent")
revisar("lavadero desde el celular", lineas[-1].endswith("LAVADERO: Sisi a qué hora"), lineas[-1:])

# Lo que no se tiene que atender
antes = len(lineas)
enviar(mensaje("m4", "outbound", texto="Sisi a qué hora", origen="business_app"), evento="whatsapp.message.sent")
revisar("duplicado ignorado", len(lineas) == antes)
enviar(mensaje("m5", "inbound", texto="chat viejo", origen="history_sync"))
revisar("historial ignorado", len(lineas) == antes)
enviar(mensaje("m6", "inbound", texto="de anteayer", timestamp=str(int(time.time()) - 2 * 86400)))
revisar("mensaje de hace 2 días ignorado", len(lineas) == antes)
enviar(mensaje("m7", "outbound"), evento="whatsapp.message.delivered")
revisar("evento de entregado ignorado", len(lineas) == antes)

# Audios, ubicación y lotes
enviar(mensaje("m8", "inbound", tipo="audio", audio={"id": "a1"},
               kapso={"direction": "inbound", "origin": "cloud_api",
                      "transcript": {"text": "tenés turnito para hoy"}}))
revisar("audio del cliente con la transcripción de Kapso",
        lineas[-1].endswith("[AUDIO transcripto, puede tener errores] tenés turnito para hoy"), lineas[-1:])
enviar(mensaje("m9", "outbound", tipo="audio", origen="business_app", audio={"id": "a2"}),
       evento="whatsapp.message.sent")
revisar("audio del lavadero transcripto con Whisper",
        "LAVADERO: [AUDIO transcripto" in lineas[-1] and "sábado" in lineas[-1].lower(), lineas[-1:])
enviar(mensaje("m10", "inbound", tipo="location",
               location={"latitude": -32.34, "longitude": -65.01, "address": "Av. Dos Venados 100"}))
revisar("ubicación con link", "[UBICACIÓN] Av. Dos Venados 100 — https://maps.google.com/?q=-32.34,-65.01"
        in lineas[-1], lineas[-1:])
enviar({"type": "whatsapp.message.received", "batch": True,
        "data": [mensaje("m11", "inbound", texto="primero"), mensaje("m12", "inbound", texto="segundo")]})
revisar("lote de dos, en orden", lineas[-2].endswith("primero") and lineas[-1].endswith("segundo"), lineas[-2:])

print("\nTodo bien." if fallas == 0 else f"\n{fallas} falla(s).")
