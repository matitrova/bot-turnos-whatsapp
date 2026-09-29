"""Prueba prompt_agente.md con conversaciones armadas, sin tocar Google Calendar.

Cada caso corre en una sesión de prueba que usa el prompt del archivo en vez del
que tiene el agente publicado, así se puede probar un cambio antes de subirlo con
actualizar_prompt.py. El calendario es de mentira: vive en memoria y se imprime.

Los casos salen de charlas reales del lavadero, con nombres y teléfonos cambiados.
Corren en paralelo y cada uno se imprime entero al final.

Uso: python probar_prompt.py [archivo.md] [parte del nombre de un caso ...]
     Sin archivo usa prompt_agente.md; sin nombres corre todos los casos.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import anthropic

load_dotenv(override=True)
cliente = anthropic.Anthropic()

ARCHIVOS = [a for a in sys.argv[1:] if a.endswith(".md")]
FILTROS = [a.lower() for a in sys.argv[1:] if not a.endswith(".md")]

with open(ARCHIVOS[0] if ARCHIVOS else "prompt_agente.md", encoding="utf-8") as archivo:
    PROMPT = archivo.read()

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


def calendario_de_prueba(turnos_previos):
    turnos = [dict(t) for t in turnos_previos]
    creados = 0

    def ejecutar(nombre, datos):
        nonlocal creados
        if nombre == "buscar_turnos":
            if not turnos:
                return "Este cliente no tiene turnos futuros."
            return "\n".join(
                f"evento_id={t['evento_id']} | inicio={t['inicio']} | {t['titulo']}" for t in turnos
            )
        if nombre == "crear_turno":
            creados += 1
            turnos.append({"evento_id": f"prueba-{creados}", "inicio": datos["inicio"], "titulo": datos["titulo"]})
            return f"Turno creado correctamente. evento_id=prueba-{creados}"
        turno = next((t for t in turnos if t["evento_id"] == datos.get("evento_id")), None)
        if nombre in ("mover_turno", "cancelar_turno") and turno is None:
            return "ERROR: ese turno no es de este cliente. No se hizo nada."
        if nombre == "mover_turno":
            turno["inicio"] = datos["nuevo_inicio"]
            return "Turno movido correctamente."
        if nombre == "cancelar_turno":
            turnos.remove(turno)
            return f"Turno cancelado correctamente: {turno['titulo']} {turno['inicio']}"
        if nombre == "avisar_al_dueno":
            return "Aviso enviado al dueño."
        return f"ERROR: la herramienta {nombre} no existe."

    return ejecutar


def enviar_linea(sesion_id, linea, ejecutar, salida):
    # Igual que hablar_con_agente() en puente.py, pero con el calendario de prueba.
    pendientes = []
    with cliente.beta.sessions.events.stream(sesion_id) as stream:
        cliente.beta.sessions.events.send(
            sesion_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": linea}]}],
        )
        for evento in stream:
            if evento.type == "agent.message":
                for bloque in evento.content:
                    salida.append(f"   AGENTE: {bloque.text}")
            elif evento.type == "agent.custom_tool_use":
                salida.append(f"   [{evento.name}] {evento.input}")
                pendientes.append(evento)
            elif evento.type == "session.status_idle":
                motivo = getattr(getattr(evento, "stop_reason", None), "type", None)
                if motivo == "requires_action" and pendientes:
                    resultados = [{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": uso.id,
                        "content": [{"type": "text", "text": ejecutar(uso.name, uso.input)}],
                    } for uso in pendientes]
                    pendientes = []
                    cliente.beta.sessions.events.send(sesion_id, events=resultados)
                else:
                    break


def correr(caso):
    salida = ["=" * 70, f"CASO: {caso['nombre']}", f"ESPERADO: {caso['esperado']}"]
    sesion = cliente.beta.sessions.create(
        agent={"type": "agent_with_overrides", "id": os.environ["AGENT_ID"], "system": PROMPT},
        environment_id=os.environ["ENVIRONMENT_ID"],
        title=f"Prueba de prompt: {caso['nombre']}",
    )
    ejecutar = calendario_de_prueba(caso.get("turnos_previos", []))
    try:
        for linea in caso["lineas"]:
            salida.append("\n>> " + linea.replace("\n", " / "))
            enviar_linea(sesion.id, linea, ejecutar, salida)
    except Exception as error:
        salida.append(f"   ERROR DE LA PRUEBA: {error}")
    finally:
        cliente.beta.sessions.archive(session_id=sesion.id)
    return "\n".join(salida)


with ThreadPoolExecutor(max_workers=7) as hilos:
    elegidos = [c for c in CASOS if not FILTROS or any(f in c["nombre"].lower() for f in FILTROS)]
    for resultado in hilos.map(correr, elegidos):
        print(resultado)
