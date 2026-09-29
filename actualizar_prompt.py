import os
from dotenv import load_dotenv
import anthropic

load_dotenv(override=True)
cliente = anthropic.Anthropic()

# El prompt vive en prompt_agente.md: se edita ahí y este script lo sube.
with open("prompt_agente.md", encoding="utf-8") as archivo:
    prompt = archivo.read()

agente = cliente.beta.agents.retrieve(os.environ["AGENT_ID"])
if agente.system == prompt:
    print("El agente ya tiene este prompt. Versión:", agente.version)
else:
    # Pasar la versión actual hace que falle si alguien lo cambió mientras tanto.
    agente = cliente.beta.agents.update(
        os.environ["AGENT_ID"], version=agente.version, system=prompt
    )
    print("Prompt actualizado. Versión nueva del agente:", agente.version)
