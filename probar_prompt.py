"""Prueba prompt_agente.md con conversaciones armadas, sin tocar Google Calendar.

Corre la misma lógica que el puente: parte cada charla en tandas según los
horarios (un chat se lee después de ESPERA sin mensajes), le pregunta al modelo
solo cuando hace falta (decidir.hay_que_evaluar) y ejecuta la decisión con
puente.aplicar() sobre un calendario de mentira. Al final muestra el costo.

Los casos salen de charlas reales del lavadero, con nombres y teléfonos cambiados.

Uso: python probar_prompt.py [archivo.md] [parte del nombre de un caso ...]
     Sin archivo usa prompt_agente.md; sin nombres corre todos los casos.
     Para probar otro modelo: MODELO=claude-haiku-4-5 python probar_prompt.py
"""
import itertools
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

os.environ["BASE_DE_DATOS"] = os.path.join(tempfile.mkdtemp(), "prueba.db")
os.environ.setdefault("KAPSO_API_KEY", "no-se-usa")
os.environ.setdefault("KAPSO_WEBHOOK_SECRET", "no-se-usa")
import puente  # noqa: E402  (carga el .env)
import decidir  # noqa: E402

ARCHIVOS = [a for a in sys.argv[1:] if a.endswith(".md")]
FILTROS = [a.lower() for a in sys.argv[1:] if not a.endswith(".md")]
if ARCHIVOS:
    with open(ARCHIVOS[0], encoding="utf-8") as archivo:
        decidir.PROMPT = archivo.read()

HOY = "martes 29/09/2026"
MANANA = "miércoles 30/09/2026"


def charla(telefono, nombre, *mensajes):
    """Arma las líneas como las arma el puente. Cada mensaje es (fecha y hora, "C" o "L", texto)."""
    return [f"[{cuando}] " + (f"CLIENTE ({telefono}, {nombre})" if quien == "C" else "LAVADERO") + f": {texto}"
            for cuando, quien, texto in mensajes]


CASOS = [
    {
        "nombre": "Saluda a Facu y no dice el servicio",
        "esperado": "CREADO mié 30/09 10:00, 120 min. Nombre Martín (no Facu). Servicio a confirmar.",
        "lineas": charla("5492664000001", "Martín",
                         (f"{HOY} 10:02", "C", "Hola facu, mañana a las 10 te lo puedo llevar?"),
                         (f"{HOY} 10:05", "L", "dale, traelo")),
    },
    {
        "nombre": "Cerámico a una pick-up",
        "esperado": "CREADO jue 01/10 09:00, día completo (hasta las 18:00 = 540 min). Tratamiento cerámico.",
        "lineas": charla("5492664000002", "Pablo R",
                         (f"{HOY} 11:20", "C", "Buenas! quería hacerle el cerámico a la hilux, el jueves a las 9 puede ser?"),
                         (f"{HOY} 11:31", "L", "Dale Pablo, te espero el jueves a las 9")),
    },
    {
        "nombre": "Pedido desde la página web",
        "esperado": "Nada hasta que acuerdan la hora. Después: CREADO sáb 03/10 09:00, 240 min, Limpieza de tapizados, nombre Lucía.",
        "lineas": charla("5492664000003", "Lu",
                         (f"{HOY} 15:40", "C", "Hola AutoShine! Quiero pedir un turno.\n\nServicio: Limpieza de tapizados\n"
                                                "Vehículo: Auto - Fiat Cronos\nCuándo: Sábado, por la mañana\n"
                                                "Mi nombre: Lucía\n\nGracias!"),
                         (f"{HOY} 16:02", "L", "Hola Lucía! El sábado a las 9 te va?"),
                         (f"{HOY} 16:10", "C", "Sii perfecto")),
    },
    {
        "nombre": "Cancela saludando a Facu",
        "esperado": "CANCELADO del turno del mié 30/09 10:00. En el aviso, el cliente NO es Facu (no tiene nombre).",
        "turnos_previos": [
            {"evento_id": "prueba-previo", "inicio": "2026-09-30T10:00:00-03:00", "titulo": "Lavado completo"},
        ],
        "lineas": charla("5492664000004", "sin nombre",
                         (f"{HOY} 19:45", "C", "Hola Facu, al final mañana no voy a poder ir, cancelame el turno porfa")),
    },
    {
        "nombre": "Contraoferta con solo la hora",
        "esperado": "Nada hasta el \"Ok. Te lo llevo\"; ahí CREADO mié 30/09 11:30 (no 10), Graciela, lavado completo 120 min.",
        "lineas": charla("5492664000005", "Graciela",
                         (f"{HOY} 17:57", "C", "Hola, cómo estás? Tendrás un turno para lavar el auto mañana? Soy Graciela, tu vecina del barrio"),
                         (f"{HOY} 18:00", "L", "Buenas como va"),
                         (f"{HOY} 18:00", "L", "Si si a qué hora"),
                         (f"{HOY} 18:07", "C", "A las 10 hs"),
                         (f"{HOY} 18:07", "C", "Está bien?"),
                         (f"{HOY} 18:17", "L", "11:30"),
                         (f"{HOY} 18:18", "C", "Ok. Te lo llevo")),
    },
    {
        "nombre": "Acepta con una condición y pide retiro",
        "esperado": "CREADO mar 29/09 14:00 con retiro a domicilio. Nombre Sofi (no trova). Sin aviso.",
        "lineas": charla("5492664000006", "Sofi",
                         (f"{HOY} 09:43", "C", "Hola trova... cómo va ? Consulta hay turno disponible para lavar el auto ??"),
                         (f"{HOY} 09:49", "L", "Buenaaas todo bien ?"),
                         (f"{HOY} 09:50", "L", "Siii para la tarde te parece ?"),
                         (f"{HOY} 09:50", "L", "Tipo 14 hs"),
                         (f"{HOY} 09:55", "C", "Lo único es que necesitaría que lo pases a buscar ... me mudé"),
                         (f"{HOY} 09:56", "L", "Sii obvio"),
                         (f"{HOY} 09:56", "L", "Pásame la ubicación"),
                         (f"{HOY} 09:58", "C", "[UBICACIÓN] Calle de Prueba 123 — https://maps.google.com/?q=-32.35,-65.01")),
    },
    {
        "nombre": "Día acordado sin hora",
        "esperado": "Ningún turno. UN solo aviso al dueño: sáb 03/10 a la mañana sin hora, cliente Nico.",
        "lineas": charla("5492664000007", "Nico",
                         (f"{HOY} 09:18", "C", "Amigoo como andas? Buen día"),
                         (f"{HOY} 09:20", "C", "Tenes turno para lavar el auto?"),
                         (f"{HOY} 09:37", "L", "Brooo buen día"),
                         (f"{HOY} 09:38", "L", "Para el sábado"),
                         (f"{HOY} 09:38", "C", "Dale amigo de una"),
                         (f"{HOY} 09:39", "L", "Te guardo?"),
                         (f"{HOY} 09:41", "C", "Si amigo"),
                         (f"{HOY} 09:41", "C", "A la mañana?"),
                         (f"{HOY} 10:05", "L", "[AUDIO]"),
                         (f"{HOY} 10:14", "C", "Dale amigo de una")),
    },
    {
        "nombre": "La hora aparece después del dale",
        "esperado": "CREADO mié 30/09 14:15, lavado de moto 90 min (XTZ 125). Sin aviso de día sin hora.",
        "lineas": charla("5492664000008", "Maxi",
                         (f"{HOY} 13:23", "C", "Consulta hermano, lavan motos?"),
                         (f"{HOY} 13:41", "L", "Buenas como va ?"),
                         (f"{HOY} 13:41", "L", "Sisi"),
                         (f"{HOY} 14:42", "C", "Perfecto que valor tiene el lavado completo?"),
                         (f"{HOY} 14:46", "C", "Y cuando te la podría llevar"),
                         (f"{HOY} 14:46", "C", "Es una xtz 125"),
                         (f"{HOY} 14:54", "L", "Te sale 10 mil el lavado"),
                         (f"{HOY} 14:54", "L", "Mañana si querés la podemos hacer"),
                         (f"{HOY} 16:03", "C", "[AUDIO]"),
                         (f"{HOY} 16:04", "L", "Dale venite a esa hora"),
                         (f"{HOY} 16:04", "C", "A las 14:15 estoy ahí")),
    },
    {
        "nombre": "Facundo cancela por la lluvia y lo pasan",
        "esperado": "CREADO mié 30/09 10:00 (lo de la lluvia es una condición). Después CANCELADO sin aviso. "
                    "Después CREADO jue 01/10 10:00. Servicio a confirmar (nadie lo dijo).",
        "lineas": charla("5492664000009", "Laura",
                         (f"{HOY} 12:46", "C", "Holaaa soy Laura, mañana a las 10 te puedo llevar la Ecosport?"),
                         (f"{HOY} 20:52", "L", "Dale traela, si llueve no abro eh"),
                         (f"{HOY} 20:54", "C", "Dale graciass"),
                         (f"{MANANA} 08:49", "L", "Buen día ! Te aviso que hoy no abro por la lluvia"),
                         (f"{MANANA} 08:57", "C", "Perfecto, gracias"),
                         (f"{MANANA} 08:57", "L", "Si querés lo hacemos mañana"),
                         (f"{MANANA} 09:02", "C", "Dale, a la misma hora?"),
                         (f"{MANANA} 09:03", "L", "Sisi")),
    },
    {
        "nombre": "Solo pregunta el precio",
        "esperado": "NADA en todo. Sin aviso.",
        "lineas": charla("5492664000010", "Mónica",
                         (f"{HOY} 10:19", "C", "Hola Facundo..como estas.."),
                         (f"{HOY} 10:19", "C", "Tendria que lavar mi auto el mieecoles que viene"),
                         (f"{HOY} 10:19", "C", "Cuanto sale??"),
                         (f"{HOY} 20:53", "L", "Buenas como va ??"),
                         (f"{HOY} 20:53", "L", "25!"),
                         (f"{MANANA} 13:09", "C", "Por ahora no..gracias")),
    },
    {
        "nombre": "Audio del cliente transcripto",
        "esperado": "CREADO mar 29/09 16:00. Nombre Marcos (no Facu). Vehículo: la chata. Servicio a confirmar.",
        "lineas": charla("5492664000011", "Marcos",
                         (f"{HOY} 13:31", "C", "[AUDIO transcripto, puede tener errores] Hola Facu, ¿cómo estás hermano? "
                                               "Che, tenés turnito para hoy, para que te dejemos la chata."),
                         (f"{HOY} 13:40", "L", "Sisi traela a las 16"),
                         (f"{HOY} 13:41", "C", "Dale, ahí vamos")),
    },
    {
        "nombre": "No hay lugar",
        "esperado": "NADA.",
        "lineas": charla("5492664000012", "Raúl",
                         (f"{HOY} 15:07", "C", "Hola buenas tardes te queda algún lugar para hoy"),
                         (f"{HOY} 15:13", "L", "Buenas ya cerré todo los turnos por hoy"),
                         (f"{HOY} 15:14", "C", "Bueno por las dudas preguntaba\nMe imagine\nGracias")),
    },
    {
        "nombre": "Confirman la hora dos veces",
        "esperado": "UN solo CREADO mar 29/09 14:00, Fiat Adventure. Después no cancela ni vuelve a crear.",
        "lineas": charla("5492664000013", "Caro",
                         (f"{HOY} 10:49", "C", "Hola Buen dia, tienen turno para las 14 hs ?"),
                         (f"{HOY} 10:56", "L", "Buen día"),
                         (f"{HOY} 10:56", "L", "Que vehículo?"),
                         (f"{HOY} 10:59", "C", "Fiat adventure, fui ayer pero olvidaron mi turno"),
                         (f"{HOY} 11:05", "L", "Si tráelo"),
                         (f"{HOY} 11:05", "L", "A qué hora ?"),
                         (f"{HOY} 11:10", "C", "14 hs ?"),
                         (f"{HOY} 11:14", "L", "Sisi"),
                         (f"{HOY} 11:15", "C", "Ok gracias")),
    },
]


# ---------- Calendario de mentira, compartido: cada caso usa su propio teléfono ----------
eventos = {}
numeros = itertools.count(1)


def insertar_evento(evento):
    evento = dict(evento, id=f"prueba-{next(numeros)}")
    eventos[evento["id"]] = evento
    return evento


puente.listar_eventos = lambda tel: sorted(
    (e for e in eventos.values() if e["extendedProperties"]["private"]["telefono"] == tel),
    key=lambda e: e["start"]["dateTime"])
puente.obtener_evento = lambda evento_id: dict(eventos[evento_id])
puente.insertar_evento = insertar_evento
puente.actualizar_evento = lambda evento: eventos.update({evento["id"]: evento})
puente.borrar_evento = lambda evento_id: eventos.pop(evento_id)
puente.avisar_al_dueno = lambda texto: None

FECHA = re.compile(r"^\[\w+ (\d\d/\d\d/\d{4} \d\d:\d\d)\]")
CLIENTE = re.compile(r"\] CLIENTE \((\d+), ([^)]*)\)")


def correr(caso):
    salida = ["=" * 70, f"CASO: {caso['nombre']}", f"ESPERADO: {caso['esperado']}"]
    telefono, nombre = next(CLIENTE.search(l).groups() for l in caso["lineas"] if CLIENTE.search(l))
    for turno in caso.get("turnos_previos", []):
        insertar_evento({"summary": turno["titulo"], "start": {"dateTime": turno["inicio"]},
                         "end": {"dateTime": turno["inicio"]},
                         "extendedProperties": {"private": {"telefono": telefono}}})

    # Partir en tandas: un mensaje a menos de ESPERA del anterior va en la misma tanda.
    tandas, anterior = [], None
    for linea in caso["lineas"]:
        cuando = datetime.strptime(FECHA.match(linea).group(1), "%d/%m/%Y %H:%M")
        if anterior is None or cuando - anterior > decidir.ESPERA:
            tandas.append([])
        tandas[-1].append(linea)
        anterior = cuando

    leidas, previas, costo_total, llamadas = [], [], 0.0, 0
    for tanda in tandas:
        for linea in tanda:
            salida.append(">> " + linea.replace("\n", " / "))
        turnos = puente.turnos_del_cliente(telefono)
        if not decidir.hay_que_evaluar(tanda, leidas, bool(turnos)):
            salida.append("   (nadie contestó todavía: no se le pregunta al modelo)")
            leidas += tanda
            continue
        ahora = datetime.strptime(FECHA.match(tanda[-1]).group(1), "%d/%m/%Y %H:%M") + decidir.ESPERA
        pedido = decidir.armar_pedido(puente.con_dia(ahora), telefono, nombre, leidas, tanda, turnos, previas)
        try:
            decision, uso = decidir.decidir(puente.cliente, pedido)
        except Exception as error:
            salida.append(f"   ERROR DE LA PRUEBA: {error}")
            break
        resultados = puente.aplicar(decision, telefono, turnos)
        costo = decidir.costo(uso)
        costo_total += costo
        llamadas += 1
        for accion in decision.acciones:
            salida.append(f"   [{accion.tipo}] " + ", ".join(
                f"{k}={v}" for k, v in accion.model_dump().items() if v is not None and k != "tipo"))
        if decision.aviso_al_dueno:
            salida.append(f"   [aviso al dueño] {decision.aviso_al_dueno}")
        salida.append(f"   RESUMEN: {decision.resumen}")
        salida.append(f"   -> {'; '.join(resultados) or 'sin cambios'}  "
                      f"(US$ {costo:.4f}; caché leída {uso.cache_read_input_tokens or 0}, "
                      f"escrita {uso.cache_creation_input_tokens or 0}, salida {uso.output_tokens})")
        aviso = f" (aviso al dueño: {decision.aviso_al_dueno})" if decision.aviso_al_dueno else ""
        previas.append(f"[{puente.con_dia(ahora)}] {decision.resumen}{aviso}")
        leidas += tanda
    salida.append(f"   TOTAL DEL CASO: {llamadas} llamada(s), US$ {costo_total:.4f}")
    return "\n".join(salida), llamadas, costo_total, len(caso["lineas"])


elegidos = [c for c in CASOS if not FILTROS or any(f in c["nombre"].lower() for f in FILTROS)]
with ThreadPoolExecutor(max_workers=7) as hilos:
    resultados = list(hilos.map(correr, elegidos))
for texto, *_ in resultados:
    print(texto)
llamadas = sum(r[1] for r in resultados)
costo = sum(r[2] for r in resultados)
mensajes = sum(r[3] for r in resultados)
print("=" * 70)
print(f"MODELO {decidir.MODELO}: {mensajes} mensajes, {llamadas} llamadas al modelo, US$ {costo:.4f} "
      f"(US$ {costo / max(mensajes, 1):.5f} por mensaje)")
