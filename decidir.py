"""La parte que decide: una sola llamada al modelo por tanda de mensajes.

La usan el puente y probar_prompt.py, así las pruebas corren la misma lógica
que producción. Acá no se toca el calendario: se devuelve qué hacer y el
puente lo ejecuta, verificando que cada turno sea del cliente que escribe.
"""
import os
from datetime import timedelta
from typing import List, Literal, Optional

import anthropic
from pydantic import BaseModel

MODELO = os.environ.get("MODELO", "claude-sonnet-5")
ESPERA = timedelta(minutes=2)       # silencio en el chat antes de leer la tanda
VENTANA = timedelta(days=7)         # cuánta charla anterior se manda como contexto
MAXIMO_DE_MENSAJES = 60
DURACION_DEL_CACHE = "1h"           # los mensajes llegan espaciados: 5 minutos no alcanza

# US$ por millón de tokens: entrada, salida, lectura de caché, escritura de caché de 1 h
PRECIOS = {
    "claude-sonnet-5": (2.00, 10.00, 0.20, 4.00),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 2.00),
}

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_agente.md"), encoding="utf-8") as archivo:
    PROMPT = archivo.read()

SIN_CONTENIDO = ("[AUDIO]", "[STICKER]", "[IMAGEN]", "[VIDEO]", "[DOCUMENT]", "[REACTION]")


class Accion(BaseModel):
    tipo: Literal["crear", "mover", "cancelar"]
    evento_id: Optional[str]          # para mover y cancelar
    inicio: Optional[str]             # AAAA-MM-DDTHH:MM, hora de Argentina, para crear y mover
    duracion_minutos: Optional[int]   # para crear y mover
    titulo: Optional[str]             # para crear
    descripcion: Optional[str]        # para crear


class Decision(BaseModel):
    acciones: List[Accion]
    aviso_al_dueno: Optional[str]
    resumen: str


def lado(linea):
    return "CLIENTE" if "] CLIENTE (" in linea else "LAVADERO"


def tiene_contenido(linea):
    texto = linea.split(": ", 1)[1] if ": " in linea else linea
    return texto.strip() not in SIN_CONTENIDO


def hay_que_evaluar(nuevos, anteriores, tiene_turnos):
    """Un turno se acuerda cuando la otra persona contesta. Por eso la tanda se
    lee solo si es una respuesta, o si el cliente ya tiene turnos (puede estar
    cancelando o cambiando). El primer mensaje de un chat es una consulta."""
    if not any(tiene_contenido(l) for l in nuevos):
        return False
    if tiene_turnos:
        return True
    if len({lado(l) for l in nuevos}) > 1:
        return True
    return bool(anteriores) and lado(anteriores[-1]) != lado(nuevos[0])


def armar_pedido(ahora, telefono, nombre, anteriores, nuevos, turnos, decisiones_previas):
    turnos_txt = "\n".join(
        f"- evento_id={t['evento_id']} | inicio={t['inicio']} | {t['titulo']}" for t in turnos
    ) or "No tiene turnos futuros."
    previas_txt = "\n".join(f"- {d}" for d in decisiones_previas) or "Nada todavía."
    lugar = max(0, MAXIMO_DE_MENSAJES - len(nuevos))
    charla = (anteriores[-lugar:] if lugar else []) + [f"NUEVO {l}" for l in nuevos]
    return (
        f"AHORA: {ahora}\n"
        f"CLIENTE: {telefono} (nombre en WhatsApp: {nombre})\n\n"
        f"TURNOS DEL CLIENTE:\n{turnos_txt}\n\n"
        f"LO QUE YA DECIDISTE EN ESTE CHAT:\n{previas_txt}\n\n"
        f"CONVERSACIÓN (los mensajes marcados NUEVO llegaron desde la última vez que la leíste):\n"
        + "\n".join(charla)
    )


def decidir(cliente, pedido):
    """Devuelve (Decision, uso). Una sola llamada; el prompt queda en caché."""
    parametros = dict(
        model=MODELO,
        max_tokens=8000,
        system=[{"type": "text", "text": PROMPT,
                 "cache_control": {"type": "ephemeral", "ttl": DURACION_DEL_CACHE}}],
        messages=[{"role": "user", "content": pedido}],
        output_format=Decision,
    )
    if not MODELO.startswith("claude-haiku-4-5"):  # Haiku 4.5 no acepta effort
        parametros["output_config"] = {"effort": "low"}
    respuesta = cliente.messages.parse(**parametros)
    if respuesta.stop_reason in ("refusal", "max_tokens") or respuesta.parsed_output is None:
        raise RuntimeError(f"El modelo no devolvió una decisión (stop_reason={respuesta.stop_reason})")
    return respuesta.parsed_output, respuesta.usage


def costo(uso, modelo=MODELO):
    entrada, salida, lectura, escritura = PRECIOS[modelo]
    return (uso.input_tokens * entrada + uso.output_tokens * salida
            + (uso.cache_read_input_tokens or 0) * lectura
            + (uso.cache_creation_input_tokens or 0) * escritura) / 1_000_000
