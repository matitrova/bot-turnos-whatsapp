import os
from dotenv import load_dotenv
import anthropic

load_dotenv(override=True)
cliente = anthropic.Anthropic()

herramientas = [
    {
        "type": "custom",
        "name": "buscar_turnos",
        "description": "Devuelve los turnos futuros del cliente de esta conversación, con su id, fecha y hora, y título. Usala siempre antes de crear (para no duplicar), mover o cancelar.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "type": "custom",
        "name": "crear_turno",
        "description": "Crea un turno en el calendario para el cliente de esta conversación.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inicio": {"type": "string", "description": "Fecha y hora de inicio, hora de Argentina, formato AAAA-MM-DDTHH:MM. Ejemplo: 2026-09-19T08:30"},
                "duracion_minutos": {"type": "integer", "description": "Duración del turno en minutos"},
                "titulo": {"type": "string", "description": "Servicio + nombre del cliente"},
                "descripcion": {"type": "string", "description": "Vehículo, servicio y mensaje de confirmación textual"},
            },
            "required": ["inicio", "duracion_minutos", "titulo", "descripcion"],
        },
    },
    {
        "type": "custom",
        "name": "mover_turno",
        "description": "Cambia el día y la hora de un turno existente del cliente de esta conversación. El evento_id se obtiene con buscar_turnos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "evento_id": {"type": "string"},
                "nuevo_inicio": {"type": "string", "description": "Formato AAAA-MM-DDTHH:MM, hora de Argentina"},
                "duracion_minutos": {"type": "integer"},
            },
            "required": ["evento_id", "nuevo_inicio", "duracion_minutos"],
        },
    },
    {
        "type": "custom",
        "name": "cancelar_turno",
        "description": "Borra un turno existente del cliente de esta conversación. El evento_id se obtiene con buscar_turnos.",
        "input_schema": {
            "type": "object",
            "properties": {"evento_id": {"type": "string"}},
            "required": ["evento_id"],
        },
    },
    {
        "type": "custom",
        "name": "avisar_al_dueno",
        "description": "Le manda un aviso al dueño del lavadero. Usala cada vez que canceles un turno.",
        "input_schema": {
            "type": "object",
            "properties": {"mensaje": {"type": "string", "description": "Nombre, teléfono, servicio, día y hora del turno cancelado, y el mensaje del cliente"}},
            "required": ["mensaje"],
        },
    },
]

agente = cliente.beta.agents.update(os.environ["AGENT_ID"], tools=herramientas)
print("Herramientas cargadas. Versión del agente:", agente.version)