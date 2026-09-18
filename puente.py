import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(override=True)
cliente = anthropic.Anthropic()
AGENT_ID = os.environ["AGENT_ID"]
ENVIRONMENT_ID = os.environ["ENVIRONMENT_ID"]
CALENDAR_ID = os.environ["CALENDAR_ID"]
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

credenciales = service_account.Credentials.from_service_account_file(
    "google-credenciales.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendario = build("calendar", "v3", credentials=credenciales)

app = Flask(__name__)

sesiones = {}              # teléfono del cliente -> sesión del agente
procesados = set()         # ids de mensajes ya procesados
candado = threading.Lock() # para que el agente atienda de a un mensaje por vez


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


# ---------- Conversación con el agente ----------

def armar_linea(mensaje, conversacion):
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

    if mensaje.get("type") == "text":
        texto = mensaje["text"]["body"]
    else:
        texto = f"[{str(mensaje.get('type')).upper()}]"

    return f"[{fecha}] {quien}: {texto}"


def hablar_con_agente(telefono, linea):
    with candado:
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

@app.route("/webhook", methods=["POST"])
def recibir_mensaje():
    tipo_evento = request.headers.get("X-Webhook-Event")
    print("Evento recibido:", tipo_evento)
    if tipo_evento and tipo_evento not in ("whatsapp.message.received", "whatsapp.message.sent"):
        return "ok", 200

    datos = request.get_json()
    mensaje = datos["message"]
    conversacion = datos["conversation"]

    if mensaje["id"] in procesados:
        print("Duplicado, lo ignoro:", mensaje["id"])
        return "ok", 200
    procesados.add(mensaje["id"])

    linea = armar_linea(mensaje, conversacion)
    print(linea)

    threading.Thread(
        target=hablar_con_agente,
        args=(conversacion["phone_number"], linea),
    ).start()
    return "ok", 200


app.run(port=8000)