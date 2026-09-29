import hashlib
import hmac
import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request
import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(override=True)
import decidir  # noqa: E402  (lee MODELO del .env)

cliente = anthropic.Anthropic()
CALENDAR_ID = os.environ["CALENDAR_ID"]
KAPSO_API_KEY = os.environ["KAPSO_API_KEY"]
KAPSO_WEBHOOK_SECRET = os.environ["KAPSO_WEBHOOK_SECRET"]
BASE_DE_DATOS = os.environ.get("BASE_DE_DATOS", "datos/bot.db")
ZONA = ZoneInfo("America/Argentina/Buenos_Aires")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MODELO_WHISPER = "modelos/ggml-small.bin"
ANTIGUEDAD_MAXIMA = timedelta(hours=24)  # los mensajes más viejos no se atienden
REINTENTO = timedelta(minutes=5)         # si falla la llamada al modelo, cuándo volver a probar

credenciales = service_account.Credentials.from_service_account_file(
    "google-credenciales.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendario = build("calendar", "v3", credentials=credenciales)

app = Flask(__name__)

procesados = set()         # ids de mensajes ya encolados
cola = queue.Queue()       # los mensajes se transcriben y guardan de a uno, en orden
reintentar_despues = {}    # teléfono -> momento a partir del cual volver a probar


# ---------- Base de datos: mensajes y decisiones sobreviven a un reinicio ----------

def base():
    conexion = sqlite3.connect(BASE_DE_DATOS)
    conexion.row_factory = sqlite3.Row
    return conexion


def crear_tablas():
    os.makedirs(os.path.dirname(BASE_DE_DATOS) or ".", exist_ok=True)
    with base() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id TEXT PRIMARY KEY, telefono TEXT, nombre TEXT, linea TEXT,
                fecha REAL, recibido REAL, leido INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS decisiones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telefono TEXT, fecha REAL, modelo TEXT,
                pedido TEXT, decision TEXT, resultados TEXT, costo REAL);
        """)


def guardar_mensaje(id, telefono, nombre, linea, fecha):
    with base() as db:
        db.execute("INSERT OR IGNORE INTO mensajes VALUES (?, ?, ?, ?, ?, ?, 0)",
                   (id, telefono, nombre, linea, fecha, time.time()))


def ya_guardado(id):
    with base() as db:
        return db.execute("SELECT 1 FROM mensajes WHERE id = ?", (id,)).fetchone() is not None


# ---------- Calendario: el puente ejecuta y controla, el modelo solo decide ----------

def a_fecha(texto):
    return datetime.fromisoformat(texto).replace(tzinfo=ZONA)


def listar_eventos(telefono):
    return calendario.events().list(
        calendarId=CALENDAR_ID,
        timeMin=datetime.now(ZONA).isoformat(),
        privateExtendedProperty=f"telefono={telefono}",
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])


def obtener_evento(evento_id):
    return calendario.events().get(calendarId=CALENDAR_ID, eventId=evento_id).execute()


def insertar_evento(evento):
    return calendario.events().insert(calendarId=CALENDAR_ID, body=evento).execute()


def actualizar_evento(evento):
    calendario.events().update(calendarId=CALENDAR_ID, eventId=evento["id"], body=evento).execute()


def borrar_evento(evento_id):
    calendario.events().delete(calendarId=CALENDAR_ID, eventId=evento_id).execute()


def turnos_del_cliente(telefono):
    return [{"evento_id": e["id"], "inicio": e["start"]["dateTime"], "titulo": e.get("summary", "")}
            for e in listar_eventos(telefono)]


def turno_del_cliente(evento_id, telefono):
    evento = obtener_evento(evento_id)
    dueno_del_turno = evento.get("extendedProperties", {}).get("private", {}).get("telefono")
    if dueno_del_turno != telefono:
        return None
    return evento


def avisar_al_dueno(texto):
    print("📣 AVISO AL DUEÑO:", texto)


def aplicar(decision, telefono, turnos):
    resultados = []
    for accion in decision.acciones:
        try:
            if accion.tipo == "crear":
                inicio = a_fecha(accion.inicio)
                if any(datetime.fromisoformat(t["inicio"]) == inicio for t in turnos):
                    resultados.append(f"crear {accion.inicio}: ya había un turno a esa hora, no se creó otro")
                    continue
                fin = inicio + timedelta(minutes=int(accion.duracion_minutos))
                creado = insertar_evento({
                    "summary": accion.titulo,
                    "description": (accion.descripcion or "") + f"\nTeléfono: {telefono}",
                    "start": {"dateTime": inicio.isoformat()},
                    "end": {"dateTime": fin.isoformat()},
                    "extendedProperties": {"private": {"telefono": telefono}},
                })
                turnos.append({"evento_id": creado["id"], "inicio": inicio.isoformat(), "titulo": accion.titulo})
                resultados.append(f"crear {accion.inicio}: creado, evento_id={creado['id']}")
            else:
                evento = turno_del_cliente(accion.evento_id, telefono)
                if evento is None:
                    resultados.append(f"{accion.tipo} {accion.evento_id}: ese turno no es de este cliente, no se hizo nada")
                    continue
                if accion.tipo == "mover":
                    inicio = a_fecha(accion.inicio)
                    fin = inicio + timedelta(minutes=int(accion.duracion_minutos))
                    evento["start"] = {"dateTime": inicio.isoformat()}
                    evento["end"] = {"dateTime": fin.isoformat()}
                    actualizar_evento(evento)
                    resultados.append(f"mover {accion.evento_id} a {accion.inicio}: movido")
                else:
                    borrar_evento(evento["id"])
                    resultados.append(f"cancelar {accion.evento_id}: cancelado")
        except Exception as error:
            resultados.append(f"{accion.tipo}: ERROR {error}")
    if decision.aviso_al_dueno:
        avisar_al_dueno(decision.aviso_al_dueno)
        resultados.append("aviso al dueño enviado")
    return resultados


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


# ---------- Mensajes ----------

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


def fecha_del_mensaje(mensaje):
    if mensaje.get("timestamp"):
        return datetime.fromtimestamp(int(mensaje["timestamp"]), ZONA)
    return datetime.now(ZONA)


def con_dia(fecha):
    return DIAS[fecha.weekday()] + " " + fecha.strftime("%d/%m/%Y %H:%M")


def armar_linea(mensaje, conversacion, phone_number_id):
    telefono = conversacion["phone_number"]
    nombre = conversacion.get("contact_name") or "sin nombre"
    if mensaje.get("kapso", {}).get("direction") == "inbound":
        quien = f"CLIENTE ({telefono}, {nombre})"
    else:
        quien = "LAVADERO"
    return f"[{con_dia(fecha_del_mensaje(mensaje))}] {quien}: {texto_del_mensaje(mensaje, phone_number_id)}"


def atender_cola():
    while True:
        mensaje, conversacion, phone_number_id = cola.get()
        try:
            linea = armar_linea(mensaje, conversacion, phone_number_id)
            print(linea)
            guardar_mensaje(mensaje["id"], conversacion["phone_number"],
                            conversacion.get("contact_name") or "sin nombre",
                            linea, fecha_del_mensaje(mensaje).timestamp())
        except Exception as error:
            print("ERROR guardando el mensaje", mensaje.get("id"), "->", error)
        finally:
            cola.task_done()


# ---------- Cuándo se le pregunta al modelo ----------

def evaluar(telefono, ahora):
    desde = ahora - decidir.VENTANA.total_seconds()
    with base() as db:
        filas = db.execute("SELECT * FROM mensajes WHERE telefono = ? AND fecha >= ? ORDER BY fecha",
                           (telefono, desde)).fetchall()
        sin_leer = db.execute("SELECT id FROM mensajes WHERE telefono = ? AND leido = 0",
                              (telefono,)).fetchall()
        previas = db.execute("SELECT fecha, decision FROM decisiones WHERE telefono = ? "
                             "ORDER BY id DESC LIMIT 5", (telefono,)).fetchall()
    ids_sin_leer = [f["id"] for f in sin_leer]
    anteriores = [f["linea"] for f in filas if f["leido"]]
    nuevos = [f["linea"] for f in filas if not f["leido"]]
    nombre = filas[-1]["nombre"] if filas else "sin nombre"

    def marcar_leidos():
        with base() as db:
            db.executemany("UPDATE mensajes SET leido = 1 WHERE id = ?", [(i,) for i in ids_sin_leer])

    turnos = turnos_del_cliente(telefono)
    if not nuevos or not decidir.hay_que_evaluar(nuevos, anteriores, bool(turnos)):
        print(f"{telefono}: nadie contestó todavía, no hace falta preguntarle al modelo")
        marcar_leidos()
        return

    decisiones_previas = []
    for previa in reversed(previas):
        d = json.loads(previa["decision"])
        cuando = con_dia(datetime.fromtimestamp(previa["fecha"], ZONA))
        aviso = f" (aviso al dueño: {d['aviso_al_dueno']})" if d.get("aviso_al_dueno") else ""
        decisiones_previas.append(f"[{cuando}] {d['resumen']}{aviso}")

    pedido = decidir.armar_pedido(con_dia(datetime.fromtimestamp(ahora, ZONA)), telefono, nombre,
                                  anteriores, nuevos, turnos, decisiones_previas)
    try:
        decision, uso = decidir.decidir(cliente, pedido)
    except Exception as error:
        print(f"{telefono}: ERROR preguntándole al modelo, reintento en 5 minutos ->", error)
        reintentar_despues[telefono] = ahora + REINTENTO.total_seconds()
        return
    resultados = aplicar(decision, telefono, turnos)
    costo = decidir.costo(uso)
    print(f"{telefono}: {decision.resumen} | {'; '.join(resultados) or 'sin cambios'} | US$ {costo:.4f}")
    with base() as db:
        db.execute("INSERT INTO decisiones (telefono, fecha, modelo, pedido, decision, resultados, costo) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (telefono, ahora, decidir.MODELO, pedido, decision.model_dump_json(),
                    json.dumps(resultados, ensure_ascii=False), costo))
    marcar_leidos()


def revisar_chats(ahora=None):
    """Lee los chats donde hay mensajes sin leer y nadie escribió en los últimos minutos."""
    ahora = ahora or time.time()
    with base() as db:
        listos = db.execute("SELECT telefono FROM mensajes WHERE leido = 0 GROUP BY telefono "
                            "HAVING MAX(recibido) <= ?", (ahora - decidir.ESPERA.total_seconds(),)).fetchall()
    for fila in listos:
        telefono = fila["telefono"]
        if reintentar_despues.get(telefono, 0) > ahora:
            continue
        try:
            evaluar(telefono, ahora)
        except Exception as error:
            print(f"{telefono}: ERROR evaluando ->", error)
            reintentar_despues[telefono] = ahora + REINTENTO.total_seconds()


def vigilar_chats():
    while True:
        revisar_chats()
        time.sleep(15)


# ---------- Webhook de Kapso ----------

def firma_valida(cuerpo, firma):
    esperada = hmac.new(KAPSO_WEBHOOK_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    return bool(firma) and hmac.compare_digest(firma, esperada)


def encolar(datos):
    mensaje = datos["message"]
    conversacion = datos["conversation"]

    if mensaje.get("kapso", {}).get("origin") == "history_sync":
        return  # chats viejos que Kapso importa: sirven para aprender, no para agendar
    if datetime.now(ZONA) - fecha_del_mensaje(mensaje) > ANTIGUEDAD_MAXIMA:
        print("Mensaje viejo, lo ignoro:", mensaje["id"])
        return
    if not conversacion.get("phone_number"):
        print("Mensaje sin teléfono del cliente, lo ignoro:", mensaje["id"])
        return
    if mensaje["id"] in procesados or ya_guardado(mensaje["id"]):
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


crear_tablas()
threading.Thread(target=atender_cola, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=vigilar_chats, daemon=True).start()
    app.run(port=8000)
