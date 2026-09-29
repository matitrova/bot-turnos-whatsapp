"""La parte que decide: una sola llamada al modelo por tanda de mensajes.

La usan el puente y probar_prompt.py, así las pruebas corren la misma lógica
que producción. Acá no se toca el calendario: se devuelve qué hacer y el
puente lo ejecuta, verificando que cada turno sea del cliente que escribe.
"""
import os
import re
import unicodedata
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

# El WhatsApp del lavadero es también el personal del dueño. Una charla se lee
# solo si en la ventana nombra algo del lavadero (o si el cliente tiene turnos).
TEMA_DEL_LAVADERO = re.compile(
    r"\b(turn|lav[aáeé]|auto(s)?\b|coche|camionet|chata|motos?\b|pick|hilux|pulid|pulir|ceramic|tapiz|"
    r"asiento|optica|faro|sellad|detail|tablero|vehicul|traela|traelo|te (lo|la) (llevo|dejo)|autoshine)")


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
    aviso_fecha: Optional[str]        # AAAA-MM-DD: el día al que se refiere el aviso
    resumen: str


def lado(linea):
    return "CLIENTE" if "] CLIENTE (" in linea else "LAVADERO"


def tiene_contenido(linea):
    texto = linea.split(": ", 1)[1] if ": " in linea else linea
    return texto.strip() not in SIN_CONTENIDO


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def es_del_lavadero(lineas):
    return any(TEMA_DEL_LAVADERO.search(sin_tildes(l.split(": ", 1)[-1])) for l in lineas)


def por_que_no_leer(nuevos, anteriores, tiene_turnos):
    """None si hay que preguntarle al modelo; si no, el motivo.

    Un turno se acuerda cuando la otra persona contesta: la tanda se lee solo si
    es una respuesta, o si el cliente ya tiene turnos (puede estar cancelando).
    El primer mensaje de un chat es una consulta. Y como el número es también el
    personal del dueño, la charla tiene que ser del lavadero."""
    if not any(tiene_contenido(l) for l in nuevos):
        return "solo stickers, fotos o audios sin transcribir"
    if tiene_turnos:
        return None
    if not es_del_lavadero(anteriores + nuevos):
        return "la charla no es del lavadero"
    if len({lado(l) for l in nuevos}) > 1:
        return None
    if anteriores and lado(anteriores[-1]) != lado(nuevos[0]):
        return None
    return "nadie contestó todavía"


def hay_que_evaluar(nuevos, anteriores, tiene_turnos):
    return por_que_no_leer(nuevos, anteriores, tiene_turnos) is None


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


def contar_decision(cuando, decision, resultados):
    """Cómo aparece una decisión anterior en LO QUE YA DECIDISTE: el resumen, el
    aviso y lo que de verdad se ejecutó, con el título de cada turno (así, si un
    turno se canceló, el modelo todavía sabe qué servicio tenía)."""
    aviso = f" (aviso al dueño: {decision.aviso_al_dueno})" if decision.aviso_al_dueno else ""
    hecho = f" — hecho: {'; '.join(r for r in resultados if not r.startswith('aviso'))}" if resultados else ""
    return f"[{cuando}] {decision.resumen}{aviso}{hecho}"


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
