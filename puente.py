import hashlib
import hmac
import os
import queue
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request
import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(override=True)
cliente = anthropic.Anthropic()
AGENT_ID = os.environ["AGENT_ID"]
ENVIRONMENT_ID = os.environ["ENVIRONMENT_ID"]
CALENDAR_ID = os.environ["CALENDAR_ID"]
KAPSO_API_KEY = os.environ["KAPSO_API_KEY"]
KAPSO_WEBHOOK_SECRET = os.environ["KAPSO_WEBHOOK_SECRET"]
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MODELO_WHISPER = "modelos/ggml-small.bin"
ANTIGUEDAD_MAXIMA = timedelta(hours=24)  # los mensajes más viejos no se atienden

credenciales = service_account.Credentials.from_service_account_file(
    "google-credenciales.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendario = build("calendar", "v3", credentials=credenciales)

app = Flask(__name__)

sesiones = {}              # teléfono del cliente -> sesión del agente
procesados = set()         # ids de mensajes ya procesados
cola = queue.Queue()       # los mensajes se atienden de a uno y en el orden en que llegaron


# ---------- Herramientas: las ejecuta el puente, no el agente ----------

def a_fecha(texto):
    return datetime.fromisoformat(texto).replace(tzinfo=ZONA)


def turno_del_cliente(evento_id, telefono):
    evento = calendario.events().get(calendarId=CALENDAR_ID, eventId=evento_id).execute()
    dueno_del_turno = evento.get("extendedProperties", {}).get("private", {}).get("telefono")
    if dueno_del_turno != telefono:
        return None
    return evento


def ejecutar_herramienta(nombre, datos, telefono):
    try:
        if nombre == "buscar_turnos":
            resultado = calendario.events().list(
                calendarId=CALENDAR_ID,
                timeMin=datetime.now(ZONA).isoformat(),
                privateExtendedProperty=f"telefono={telefono}",
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            turnos = resultado.get("items", [])
            if not turnos:
                return "Este cliente no tiene turnos futuros."
            return "\n".join(
                f"evento_id={t['id']} | inicio={t['start']['dateTime']} | {t.get('summary', '')}"
                for t in turnos
            )

        if nombre == "crear_turno":
            inicio = a_fecha(datos["inicio"])
            fin = inicio + timedelta(minutes=int(datos["duracion_minutos"]))
            evento = {
                "summary": datos["titulo"],
                "description": datos["descripcion"] + f"\nTeléfono: {telefono}",
                "start": {"dateTime": inicio.isoformat()},
                "end": {"dateTime": fin.isoformat()},
                "extendedProperties": {"private": {"telefono": telefono}},
            }
            creado = calendario.events().insert(calendarId=CALENDAR_ID, body=evento).execute()
            return f"Turno creado correctamente. evento_id={creado['id']}"

        if nombre == "mover_turno":
            evento = turno_del_cliente(datos["evento_id"], telefono)
            if evento is None:
                return "ERROR: ese turno no es de este cliente. No se hizo nada."
            inicio = a_fecha(datos["nuevo_inicio"])
            fin = inicio + timedelta(minutes=int(datos["duracion_minutos"]))
            evento["start"] = {"dateTime": inicio.isoformat()}
            evento["end"] = {"dateTime": fin.isoformat()}
            calendario.events().update(calendarId=CALENDAR_ID, eventId=evento["id"], body=evento).execute()
            return "Turno movido correctamente."

        if nombre == "cancelar_turno":
            evento = turno_del_cliente(datos["evento_id"], telefono)
            if evento is None:
                return "ERROR: ese turno no es de este cliente. No se hizo nada."
            calendario.events().delete(calendarId=CALENDAR_ID, eventId=evento["id"]).execute()
            return f"Turno cancelado correctamente: {evento.get('summary', '')} {evento['start']['dateTime']}"

        if nombre == "avisar_al_dueno":
            print("📣 AVISO AL DUEÑO:", datos["mensaje"])
            return "Aviso enviado al dueño."

        return f"ERROR: la herramienta {nombre} no existe."

    except Exception as error:
        return f"ERROR: {error}"


# ---------- Audios ----------

def transcribir_archivo(ruta):
    with tempfile.TemporaryDirectory() as carpeta:
        wav = os.path.join(carpeta, "audio.wav")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", ruta, "-ar", "16000", "-ac", "1", wav],
                       check=True)
        salida = subprocess.run(["whisper-cli", "-m", MODELO_WHISPER, "-l", "es", "-nt", "-np", "-f", wav],
                                check=True, capture_output=True, text=True)
    return " ".join(salida.stdout.split())


def transcribir_audio(mensaje, phone_number_id):
    # Kapso transcribe solo los audios que llegan de los clientes. Los que manda
    # el lavadero desde su celular se bajan de Kapso y se transcriben acá.
    transcripcion = (mensaje.get("kapso", {}).get("transcript") or {}).get("text")
    if transcripcion:
        return transcripcion
    media = requests.get(
        f"https://api.kapso.ai/meta/whatsapp/v24.0/{mensaje['audio']['id']}",
        params={"phone_number_id": phone_number_id},
        headers={"X-API-Key": KAPSO_API_KEY},
        timeout=30,
    )
    media.raise_for_status()
    audio = requests.get(media.json()["download_url"], timeout=60)
    audio.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".ogg") as archivo:
        archivo.write(audio.content)
        archivo.flush()
        return transcribir_archivo(archivo.name)


# ---------- Conversación con el agente ----------

def texto_del_mensaje(mensaje, phone_number_id):
    tipo = mensaje.get("type")
    if tipo == "text":
        return mensaje["text"]["body"]
    if tipo == "audio":
        try:
            return "[AUDIO transcripto, puede tener errores] " + transcribir_audio(mensaje, phone_number_id)
        except Exception as error:
            print("No se pudo transcribir el audio:", error)
            return "[AUDIO]"
    if tipo == "location":
        lugar = mensaje["location"]
        partes = [lugar.get("name"), lugar.get("address"),
                  f"https://maps.google.com/?q={lugar['latitude']},{lugar['longitude']}"]
        return "[UBICACIÓN] " + " — ".join(p for p in partes if p)
    if tipo == "image" and mensaje.get("image", {}).get("caption"):
        return "[IMAGEN] " + mensaje["image"]["caption"]
    return f"[{str(tipo).upper()}]"


def armar_linea(mensaje, conversacion, phone_number_id):
    if mensaje.get("timestamp"):
        fecha = datetime.fromtimestamp(int(mensaje["timestamp"]), ZONA)
    else:
        fecha = datetime.now(ZONA)
    fecha = DIAS[fecha.weekday()] + " " + fecha.strftime("%d/%m/%Y %H:%M")

    telefono = conversacion["phone_number"]
    nombre = conversacion.get("contact_name") or "sin nombre"

    direccion = mensaje.get("kapso", {}).get("direction")
    if direccion == "inbound":
        quien = f"CLIENTE ({telefono}, {nombre})"
    else:
        quien = "LAVADERO"

    return f"[{fecha}] {quien}: {texto_del_mensaje(mensaje, phone_number_id)}"


def hablar_con_agente(telefono, linea):
    if telefono not in sesiones:
        sesion = cliente.beta.sessions.create(
            agent=AGENT_ID,
            environment_id=ENVIRONMENT_ID,
            title=f"WhatsApp {telefono}",
        )
        sesiones[telefono] = sesion.id
        print("Sesión nueva para", telefono, "->", sesion.id)
    sesion_id = sesiones[telefono]

    pendientes = []
    with cliente.beta.sessions.events.stream(sesion_id) as stream:
        cliente.beta.sessions.events.send(
            sesion_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": linea}]}],
        )
        for evento in stream:
            print("   evento:", evento.type)

            if evento.type == "agent.message":
                for bloque in evento.content:
                    print("AGENTE:", bloque.text)

            elif evento.type == "agent.custom_tool_use":
                print(f"[Herramienta: {evento.name}] {evento.input}")
                pendientes.append(evento)

            elif evento.type == "session.status_idle":
                motivo = getattr(getattr(evento, "stop_reason", None), "type", None)
                print("   motivo:", motivo)
                if motivo == "requires_action" and pendientes:
                    resultados = []
                    for uso in pendientes:
                        resultado = ejecutar_herramienta(uso.name, uso.input, telefono)
                        print("   ->", resultado)
                        resultados.append({
                            "type": "user.custom_tool_result",
                            "custom_tool_use_id": uso.id,
                            "content": [{"type": "text", "text": resultado}],
                        })
                    pendientes = []
                    cliente.beta.sessions.events.send(sesion_id, events=resultados)
                else:
                    break


# ---------- Webhook de Kapso ----------

def atender_cola():
    while True:
        mensaje, conversacion, phone_number_id = cola.get()
        try:
            linea = armar_linea(mensaje, conversacion, phone_number_id)
            print(linea)
            hablar_con_agente(conversacion["phone_number"], linea)
        except Exception as error:
            print("ERROR atendiendo el mensaje", mensaje.get("id"), "->", error)
        finally:
            cola.task_done()


def firma_valida(cuerpo, firma):
    esperada = hmac.new(KAPSO_WEBHOOK_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    return bool(firma) and hmac.compare_digest(firma, esperada)


def encolar(datos):
    mensaje = datos["message"]
    conversacion = datos["conversation"]

    if mensaje.get("kapso", {}).get("origin") == "history_sync":
        return  # chats viejos que Kapso importa: sirven para aprender, no para agendar
    if mensaje.get("timestamp"):
        enviado = datetime.fromtimestamp(int(mensaje["timestamp"]), ZONA)
        if datetime.now(ZONA) - enviado > ANTIGUEDAD_MAXIMA:
            print("Mensaje viejo, lo ignoro:", mensaje["id"], enviado)
            return
    if not conversacion.get("phone_number"):
        print("Mensaje sin teléfono del cliente, lo ignoro:", mensaje["id"])
        return
    if mensaje["id"] in procesados:
        print("Duplicado, lo ignoro:", mensaje["id"])
        return
    procesados.add(mensaje["id"])
    cola.put((mensaje, conversacion, datos.get("phone_number_id")))


@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    cuerpo = request.get_data()
    if not firma_valida(cuerpo, request.headers.get("X-Webhook-Signature")):
        print("Firma inválida: no viene de Kapso, lo rechazo")
        return "firma invalida", 401

    tipo_evento = request.headers.get("X-Webhook-Event")
    print("Evento recibido:", tipo_evento)
    if tipo_evento and tipo_evento not in ("whatsapp.message.received", "whatsapp.message.sent"):
        return "ok", 200

    datos = request.get_json()
    # Si en Kapso se activa el "buffering", los mensajes llegan de a varios.
    for item in (datos["data"] if datos.get("batch") else [datos]):
        encolar(item)
    return "ok", 200


threading.Thread(target=atender_cola, daemon=True).start()

if __name__ == "__main__":
    app.run(port=8000)
