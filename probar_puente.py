"""Prueba el puente sin Kapso, sin la API de Anthropic y sin Google Calendar.

Manda webhooks armados a mano y revisa qué se guarda, cuándo se le pregunta al
modelo, qué recibe y qué se ejecuta. El modelo y el calendario son de mentira;
la descarga de audios de Kapso también. La transcripción con Whisper es la real
(el audio de prueba lo genera el comando `say` de la Mac).
"""
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace

CARPETA = tempfile.mkdtemp()
os.environ["BASE_DE_DATOS"] = os.path.join(CARPETA, "prueba.db")
os.environ.setdefault("KAPSO_API_KEY", "clave-de-prueba")
os.environ.setdefault("KAPSO_WEBHOOK_SECRET", "secreto-de-prueba")
import puente  # noqa: E402
from decidir import Accion, Decision, ESPERA, hay_que_evaluar  # noqa: E402

# ---------- Calendario de mentira ----------
eventos = {}


def listar_eventos(telefono):
    return [e for e in eventos.values() if e["extendedProperties"]["private"]["telefono"] == telefono]


def insertar_evento(evento):
    evento = dict(evento, id=f"ev{len(eventos) + 1}")
    eventos[evento["id"]] = evento
    return evento


puente.listar_eventos = listar_eventos
puente.obtener_evento = lambda evento_id: dict(eventos[evento_id])
puente.insertar_evento = insertar_evento
puente.actualizar_evento = lambda evento: eventos.update({evento["id"]: evento})
puente.borrar_evento = lambda evento_id: eventos.pop(evento_id)

# ---------- Modelo de mentira ----------
pedidos = []
respuestas = []   # lo que "decide" el modelo en cada llamada, en orden


def decidir_falso(cliente, pedido):
    pedidos.append(pedido)
    respuesta = respuestas.pop(0)
    if isinstance(respuesta, Exception):
        raise respuesta
    uso = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=4000,
                          cache_creation_input_tokens=0)
    return respuesta, uso


puente.decidir.decidir = decidir_falso


def crear(inicio, titulo="Lavado completo - Prueba"):
    return Accion(tipo="crear", evento_id=None, inicio=inicio, duracion_minutos=120, titulo=titulo,
                  descripcion="prueba")


def decision(*acciones, aviso=None, resumen="NADA"):
    return Decision(acciones=list(acciones), aviso_al_dueno=aviso, resumen=resumen)


# ---------- Audio de prueba y descarga de Kapso de mentira ----------
AUDIO_DE_PRUEBA = os.path.join(CARPETA, "audio.aiff")
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

# ---------- Webhooks ----------
web = puente.app.test_client()


def mensaje(id, direccion, tipo="text", texto="hola", origen="cloud_api", timestamp=None,
            telefono="5492660000000", **extra):
    datos = {
        "message": {"id": id, "timestamp": timestamp or str(int(time.time())), "type": tipo,
                    "kapso": {"direction": direccion, "origin": origen}},
        "conversation": {"phone_number": telefono, "contact_name": "Cliente de prueba"},
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


def lineas(telefono="5492660000000"):
    with puente.base() as db:
        return [f["linea"] for f in db.execute("SELECT linea FROM mensajes WHERE telefono = ? ORDER BY fecha, rowid",
                                               (telefono,))]


def mas_tarde(minutos=0):
    return time.time() + ESPERA.total_seconds() + 1 + minutos * 60


fallas = 0


def revisar(nombre, condicion, detalle=""):
    global fallas
    print(("OK   " if condicion else "FALLA"), nombre, "" if condicion else detalle)
    fallas += 0 if condicion else 1


# ---------- Seguridad ----------
revisar("sin firma -> 401", enviar(mensaje("m1", "inbound"), firma=False) == 401)
r = web.post("/webhook", data=json.dumps(mensaje("m2", "inbound")),
             headers={"X-Webhook-Event": "whatsapp.message.received", "X-Webhook-Signature": "0" * 64,
                      "Content-Type": "application/json"})
revisar("firma equivocada -> 401", r.status_code == 401)
revisar("nada de eso se guardó", lineas() == [], lineas())

# ---------- Qué se guarda ----------
enviar(mensaje("m3", "inbound", texto="Hola Facu, tenés turno mañana?"))
revisar("cliente", lineas()[-1].endswith("CLIENTE (5492660000000, Cliente de prueba): Hola Facu, tenés turno mañana?"),
        lineas()[-1:])
enviar(mensaje("m4", "outbound", texto="Sisi a qué hora", origen="business_app"), evento="whatsapp.message.sent")
revisar("lavadero desde el celular", lineas()[-1].endswith("LAVADERO: Sisi a qué hora"), lineas()[-1:])

antes = len(lineas())
enviar(mensaje("m4", "outbound", texto="Sisi a qué hora", origen="business_app"), evento="whatsapp.message.sent")
revisar("duplicado ignorado", len(lineas()) == antes)
enviar(mensaje("m5", "inbound", texto="chat viejo", origen="history_sync"))
revisar("historial ignorado", len(lineas()) == antes)
enviar(mensaje("m6", "inbound", texto="de anteayer", timestamp=str(int(time.time()) - 2 * 86400)))
revisar("mensaje de hace 2 días ignorado", len(lineas()) == antes)
enviar(mensaje("m7", "outbound"), evento="whatsapp.message.delivered")
revisar("evento de entregado ignorado", len(lineas()) == antes)

T = "5492661111111"
enviar(mensaje("a1", "inbound", tipo="audio", audio={"id": "x1"}, telefono=T,
               kapso={"direction": "inbound", "origin": "cloud_api", "transcript": {"text": "tenés turnito para hoy"}}))
revisar("audio del cliente con la transcripción de Kapso",
        lineas(T)[-1].endswith("[AUDIO transcripto, puede tener errores] tenés turnito para hoy"), lineas(T)[-1:])
enviar(mensaje("a2", "outbound", tipo="audio", origen="business_app", audio={"id": "x2"}, telefono=T),
       evento="whatsapp.message.sent")
revisar("audio del lavadero transcripto con Whisper",
        "LAVADERO: [AUDIO transcripto" in lineas(T)[-1] and "sábado" in lineas(T)[-1].lower(), lineas(T)[-1:])
enviar(mensaje("a3", "inbound", tipo="location", telefono=T,
               location={"latitude": -32.34, "longitude": -65.01, "address": "Av. Dos Venados 100"}))
revisar("ubicación con link", "[UBICACIÓN] Av. Dos Venados 100 — https://maps.google.com/?q=-32.34,-65.01"
        in lineas(T)[-1], lineas(T)[-1:])
enviar({"type": "whatsapp.message.received", "batch": True,
        "data": [mensaje("a4", "inbound", texto="primero", telefono=T),
                 mensaje("a5", "inbound", texto="segundo", telefono=T)]})
revisar("lote de dos, en orden", lineas(T)[-2].endswith("primero") and lineas(T)[-1].endswith("segundo"),
        lineas(T)[-2:])
respuestas.extend([decision(), decision()])
puente.revisar_chats(mas_tarde())  # deja leídos estos dos chats para que no molesten abajo

# ---------- Cuándo se le pregunta al modelo ----------
revisar("regla: primer mensaje de un chat no", not hay_que_evaluar(["[x] CLIENTE (1, a): hola"], [], False))
revisar("regla: respuesta del otro lado sí",
        hay_que_evaluar(["[x] LAVADERO: dale"], ["[x] CLIENTE (1, a): mañana a las 10?"], False))
revisar("regla: el mismo lado insiste, no",
        not hay_que_evaluar(["[x] CLIENTE (1, a): ?"], ["[x] CLIENTE (1, a): hay turno?"], False))
revisar("regla: con un turno agendado, sí", hay_que_evaluar(["[x] CLIENTE (1, a): cancelame"], [], True))
revisar("regla: solo un sticker, no",
        not hay_que_evaluar(["[x] CLIENTE (1, a): [STICKER]"], ["[x] LAVADERO: dale"], True))

C = "5492662222222"
enviar(mensaje("c1", "inbound", texto="Mañana a las 10 te lo puedo llevar?", telefono=C))
llamadas = len(pedidos)
puente.revisar_chats(time.time())
revisar("recién llegado: todavía espera", len(pedidos) == llamadas)
puente.revisar_chats(mas_tarde())
revisar("primer mensaje del chat: no llama al modelo", len(pedidos) == llamadas)

enviar(mensaje("c2", "outbound", texto="dale, traelo", origen="business_app", telefono=C),
       evento="whatsapp.message.sent")
respuestas.append(decision(crear("2026-09-30T10:00"), resumen="CREADO: mié 30/09 10:00"))
puente.revisar_chats(mas_tarde())
revisar("la respuesta del lavadero sí llama al modelo, una vez", len(pedidos) == llamadas + 1)
pedido = pedidos[-1]
revisar("el pedido trae la charla y marca lo nuevo",
        "CLIENTE (5492662222222" in pedido and "NUEVO [" in pedido and "LAVADERO: dale, traelo" in pedido
        and "NUEVO [" not in pedido.split("Mañana a las 10")[0].split("CONVERSACIÓN")[1], pedido)
revisar("se creó el turno con el teléfono del cliente",
        len(listar_eventos(C)) == 1 and listar_eventos(C)[0]["start"]["dateTime"].startswith("2026-09-30T10:00"),
        eventos)

enviar(mensaje("c3", "inbound", texto="Genial, gracias!", telefono=C))
respuestas.append(decision(crear("2026-09-30T10:00"), resumen="CREADO otra vez"))
puente.revisar_chats(mas_tarde())
pedido = pedidos[-1]
revisar("con turno agendado, el cliente sí llama al modelo", len(pedidos) == llamadas + 2)
revisar("el pedido trae los turnos y lo ya decidido",
        "evento_id=ev" in pedido and "CREADO: mié 30/09 10:00" in pedido.split("LO QUE YA DECIDISTE")[1], pedido)
revisar("no duplica un turno a la misma hora", len(listar_eventos(C)) == 1, eventos)

OTRO = "5492663333333"
ajeno = insertar_evento({"summary": "de otro", "start": {"dateTime": "2026-10-01T10:00:00-03:00"},
                         "end": {"dateTime": "2026-10-01T12:00:00-03:00"},
                         "extendedProperties": {"private": {"telefono": OTRO}}})
enviar(mensaje("c4", "inbound", texto="cancelá el turno de mi vecino", telefono=C))
respuestas.append(decision(Accion(tipo="cancelar", evento_id=ajeno["id"], inicio=None, duracion_minutos=None,
                                  titulo=None, descripcion=None), resumen="CANCELADO"))
puente.revisar_chats(mas_tarde())
revisar("no toca turnos de otro cliente aunque el modelo lo pida", ajeno["id"] in eventos)

enviar(mensaje("c5", "inbound", texto="al final no voy, cancelame", telefono=C))
respuestas.append(RuntimeError("sin crédito"))
puente.revisar_chats(mas_tarde())
llamadas = len(pedidos)
puente.revisar_chats(mas_tarde())
revisar("si falla el modelo, no reintenta enseguida", len(pedidos) == llamadas)
respuestas.append(decision(Accion(tipo="cancelar", evento_id=listar_eventos(C)[0]["id"], inicio=None,
                                  duracion_minutos=None, titulo=None, descripcion=None),
                           aviso="Canceló el cliente", resumen="CANCELADO"))
puente.revisar_chats(mas_tarde(minutos=6))
revisar("a los 5 minutos reintenta y los mensajes no se perdieron",
        len(pedidos) == llamadas + 1 and "NUEVO" in pedidos[-1] and "cancelame" in pedidos[-1])
revisar("canceló el turno del cliente", listar_eventos(C) == [], eventos)

with puente.base() as db:
    guardadas = db.execute("SELECT COUNT(*) FROM decisiones WHERE telefono = ?", (C,)).fetchone()[0]
revisar("cada decisión queda guardada para revisar después", guardadas == 4, guardadas)

print("\nTodo bien." if fallas == 0 else f"\n{fallas} falla(s).")
