"""Prueba prompt_agente.md con conversaciones armadas, sin tocar Google Calendar.

Cada caso corre en una sesión de prueba que usa el prompt del archivo en vez del
que tiene el agente publicado, así se puede probar un cambio antes de subirlo con
actualizar_prompt.py. El calendario es de mentira: vive en memoria y se imprime.

Uso: python probar_prompt.py [archivo]   (por defecto, prompt_agente.md)
"""
import os
import sys
from dotenv import load_dotenv
import anthropic

load_dotenv(override=True)
cliente = anthropic.Anthropic()

with open(sys.argv[1] if len(sys.argv) > 1 else "prompt_agente.md", encoding="utf-8") as archivo:
    PROMPT = archivo.read()

CASOS = [
    {
        "nombre": "Saluda a Facu y no dice el servicio",
        "esperado": "CREADO mié 30/09 10:00, 120 min. Nombre Martín (no Facu). Servicio a confirmar.",
        "lineas": [
            "[martes 29/09/2026 10:02] CLIENTE (5492664000001, Martín): Hola facu, mañana a las 10 te lo puedo llevar?",
            "[martes 29/09/2026 10:05] LAVADERO: dale, traelo",
        ],
    },
    {
        "nombre": "Cerámico a una pick-up",
        "esperado": "CREADO jue 01/10 09:00, día completo (hasta las 18:00 = 540 min). Tratamiento cerámico.",
        "lineas": [
            "[martes 29/09/2026 11:20] CLIENTE (5492664000002, Pablo R): Buenas! quería hacerle el cerámico a la hilux, el jueves a las 9 puede ser?",
            "[martes 29/09/2026 11:31] LAVADERO: Dale Pablo, te espero el jueves a las 9",
        ],
    },
    {
        "nombre": "Pedido desde la página web",
        "esperado": "Nada hasta que acuerdan la hora. Después: CREADO sáb 03/10 09:00, 240 min, Limpieza de tapizados, nombre Lucía.",
        "lineas": [
            "[martes 29/09/2026 15:40] CLIENTE (5492664000003, Lu): Hola AutoShine! Quiero pedir un turno.\n\n"
            "Servicio: Limpieza de tapizados\nVehículo: Auto - Fiat Cronos\nCuándo: Sábado, por la mañana\n"
            "Mi nombre: Lucía\n\nGracias!",
            "[martes 29/09/2026 16:02] LAVADERO: Hola Lucía! El sábado a las 9 te va?",
            "[martes 29/09/2026 16:10] CLIENTE (5492664000003, Lu): Sii perfecto",
        ],
    },
    {
        "nombre": "Cancela saludando a Facu",
        "esperado": "CANCELADO del turno del mié 30/09 10:00. En el aviso, el cliente NO es Facu (no tiene nombre).",
        "turnos_previos": [
            {"evento_id": "prueba-previo", "inicio": "2026-09-30T10:00:00-03:00",
             "titulo": "Lavado completo"},
        ],
        "lineas": [
            "[martes 29/09/2026 19:45] CLIENTE (5492664000004, sin nombre): Hola Facu, al final mañana no voy a poder ir, cancelame el turno porfa",
        ],
    },
]


def calendario_de_prueba(turnos_previos):
    turnos = list(turnos_previos)

    def ejecutar(nombre, datos):
        if nombre == "buscar_turnos":
            if not turnos:
                return "Este cliente no tiene turnos futuros."
            return "\n".join(
                f"evento_id={t['evento_id']} | inicio={t['inicio']} | {t['titulo']}" for t in turnos
            )
        if nombre == "crear_turno":
            turnos.append({"evento_id": f"prueba-{len(turnos) + 1}",
                           "inicio": datos["inicio"], "titulo": datos["titulo"]})
            return f"Turno creado correctamente. evento_id=prueba-{len(turnos)}"
        if nombre == "mover_turno":
            return "Turno movido correctamente."
        if nombre == "cancelar_turno":
            return "Turno cancelado correctamente."
        if nombre == "avisar_al_dueno":
            return "Aviso enviado al dueño."
        return f"ERROR: la herramienta {nombre} no existe."

    return ejecutar


def enviar_linea(sesion_id, linea, ejecutar):
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
                    print("   AGENTE:", bloque.text)
            elif evento.type == "agent.custom_tool_use":
                print(f"   [{evento.name}] {evento.input}")
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


for caso in CASOS:
    print("=" * 70)
    print("CASO:", caso["nombre"])
    print("ESPERADO:", caso["esperado"])
    sesion = cliente.beta.sessions.create(
        agent={"type": "agent_with_overrides", "id": os.environ["AGENT_ID"], "system": PROMPT},
        environment_id=os.environ["ENVIRONMENT_ID"],
        title=f"Prueba de prompt: {caso['nombre']}",
    )
    ejecutar = calendario_de_prueba(caso.get("turnos_previos", []))
    try:
        for linea in caso["lineas"]:
            print("\n>>", linea.replace("\n", " / "))
            enviar_linea(sesion.id, linea, ejecutar)
    finally:
        cliente.beta.sessions.archive(session_id=sesion.id)
