import os
from dotenv import load_dotenv
import anthropic

load_dotenv()
cliente = anthropic.Anthropic()

sesion = cliente.beta.sessions.create(
    agent=os.environ["AGENT_ID"],
    environment_id=os.environ["ENVIRONMENT_ID"],
    title="Prueba desde la compu",
)
print("Sesión creada:", sesion.id)

conversacion = """[viernes 18/09/2026 13:54] CLIENTE (542665298746, BotTito): Hola, mañana 8.30 te lo puedo dejar?
[viernes 18/09/2026 13:54] LAVADERO: dale, dejamelo con la llave puesta
[viernes 18/09/2026 13:55] CLIENTE (542665298746, BotTito): Dale, mañana la dejo. Gracias"""

with cliente.beta.sessions.events.stream(sesion.id) as stream:
    cliente.beta.sessions.events.send(
        sesion.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": conversacion}]}],
    )
    for evento in stream:
        if evento.type == "agent.message":
            for bloque in evento.content:
                print(bloque.text, end="")
        elif evento.type == "agent.tool_use":
            print("\n[El agente quiso usar la herramienta:", evento.name, "]")
        elif evento.type == "session.status_idle":
            print("\n\nEl agente terminó.")
            break