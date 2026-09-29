# Bot de turnos por WhatsApp

Un bot de IA que agenda solo los turnos de un lavadero de autos. Es pasivo:
nunca le contesta al cliente. Lee la conversación de WhatsApp entre el cliente y
el dueño, que sigue atendiendo desde su celular como siempre, y cuando los dos
acuerdan un día y una hora, crea, mueve o cancela el turno en Google Calendar.
Nadie carga nada a mano.

## Cómo funciona

```
WhatsApp  →  Kapso  →  puente (Flask + SQLite)  →  modelo (1 llamada por tanda)
                              │                           │
                              │      decisión (JSON)      │
                              ← ─────────────────────────-┘
                              │
                              └→  Google Calendar (lo ejecuta el puente)
```

1. Kapso manda cada mensaje al puente (`puente.py`), que lo guarda en una base
   SQLite: los del cliente y los que el dueño manda desde su celular.
2. Cuando un chat queda **2 minutos sin mensajes nuevos**, el puente decide si
   vale la pena leerlo. Un turno se acuerda cuando la otra persona contesta, así
   que solo lo lee si la tanda es una respuesta, o si el cliente ya tiene un
   turno (puede estar cancelando). El primer mensaje de un chat es una consulta
   y no gasta nada.
3. Si hay que leerlo, hace **una sola llamada** al modelo (`decidir.py`) con los
   turnos que ya tiene el cliente, las decisiones anteriores de ese chat y la
   charla de los últimos 7 días, con los mensajes nuevos marcados.
4. El modelo contesta en un formato fijo: qué crear, mover o cancelar, si hay
   que avisarle al dueño y un resumen. **El puente lo ejecuta**, con sus
   controles.

## La seguridad no está en el prompt

Un modelo hace lo que el prompt le pide, salvo cuando no. Si la única defensa
contra "cancelame el turno de Juan" fuera una instrucción escrita en el prompt,
alcanzaría con insistir para saltearla.

Acá no depende del prompt:

- Cada turno guarda el teléfono de su dueño en `extendedProperties.private`.
- El puente le pasa al modelo solo los turnos de quien está escribiendo. Los
  demás no existen para él.
- Antes de mover o cancelar, el puente verifica que el evento sea de ese
  teléfono. Si el modelo pide tocar otro, no se hace nada.
- Antes de crear, el puente verifica que ese cliente no tenga ya un turno a esa
  hora.

El modelo no puede saltear los controles porque viven en el código que ejecuta,
no en el texto que el modelo lee.

## Por qué gasta poco

Antes era un agente con herramientas: cada mensaje despertaba una sesión que
releía toda la charla, y cada herramienta era otra vuelta al modelo. En las 13
conversaciones de prueba eso eran 73 turnos del agente, con 2 o 3 llamadas cada
uno. Ahora son **29 llamadas**, porque:

- los mensajes seguidos se leen juntos (2 minutos de silencio);
- no se llama al modelo si nadie contestó todavía;
- una sola llamada por tanda: los turnos del cliente ya van en el pedido;
- solo va la charla de los últimos 7 días, no todo el historial;
- el prompt queda en caché una hora (`DURACION_DEL_CACHE`), porque los mensajes
  llegan espaciados y la caché de 5 minutos casi nunca se aprovecharía.

Cada decisión queda en la tabla `decisiones` con lo que se le mandó al modelo,
lo que contestó, lo que se ejecutó y cuánto costó. Sirve para revisar errores y
convertirlos en casos de prueba.

## Otros detalles del puente

- **Solo acepta mensajes de Kapso**: verifica la firma `X-Webhook-Signature`
  (HMAC-SHA256 del cuerpo con `KAPSO_WEBHOOK_SECRET`). Sin eso, cualquiera que
  conociera la URL podría crear o cancelar turnos con mensajes falsos.
- **Sobrevive a un reinicio**: mensajes y decisiones viven en SQLite
  (`datos/bot.db`), no en memoria.
- **Deduplicación**: los webhooks se reintentan; un mensaje ya guardado se
  ignora.
- **En orden**: los mensajes se transcriben y guardan de a uno, en el orden en
  que llegaron.
- **Si falla el modelo** (por ejemplo, sin crédito), los mensajes quedan sin
  leer y se reintenta a los 5 minutos. No se pierde nada.
- **No agenda el pasado**: ignora los chats viejos que Kapso importa
  (`origin: history_sync`) y cualquier mensaje de más de 24 horas. Los chats
  viejos sirven para sacar casos de prueba, no para crear turnos.
- **Audios de los dos lados**: los del cliente llegan transcriptos por Kapso.
  Los que el dueño manda desde su celular no, así que el puente los baja de
  Kapso y los transcribe con Whisper (`whisper-cli` + `modelos/ggml-small.bin`).
- **Ubicaciones** con la dirección y un link de Google Maps, para los retiros a
  domicilio.
- **Zona horaria explícita** (`America/Argentina/Buenos_Aires`): los turnos se
  crean en la hora local, no en UTC.

## Archivos

- `puente.py` — webhook, base de datos, cuándo leer cada chat y ejecución en el
  calendario.
- `decidir.py` — la llamada al modelo: cuándo hace falta, qué se le manda y qué
  contesta. La usan el puente y las pruebas.
- `prompt_agente.md` — el prompt.
- `probar_prompt.py` — corre 13 conversaciones armadas con la misma lógica que
  el puente y un calendario de mentira; muestra decisiones y costo.
  `python probar_prompt.py lluvia` corre solo los casos cuyo nombre lo contiene;
  `MODELO=claude-haiku-4-5 python probar_prompt.py` prueba otro modelo.
- `probar_puente.py` — prueba el puente (firma, duplicados, historial, audios,
  cuándo se llama al modelo, controles de seguridad, reintentos) sin Kapso, sin
  la API de Anthropic y sin Google Calendar.
- `probar_calendario.py` — prueba aislada de la conexión con Google Calendar.

No van al repo: `conversaciones-reales/` (charlas y audios de clientes, datos
personales), `datos/` (la base del puente) y `modelos/` (Whisper, 466 MB).

## Cómo correrlo

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask anthropic python-dotenv requests pydantic google-api-python-client google-auth

brew install ffmpeg whisper-cpp   # para transcribir los audios del dueño
mkdir -p modelos && curl -L -o modelos/ggml-small.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin

cp .env.example .env        # completar con los valores reales
# y dejar el JSON de la cuenta de servicio de Google como google-credenciales.json

python probar_puente.py     # no gasta nada
python puente.py            # escucha en el puerto 8000
ngrok http 8000             # para exponerlo a Kapso durante el desarrollo
```

El calendario del dueño tiene que estar compartido con el email de la cuenta de
servicio.

En Kapso, el webhook se crea **en el número** (no en el proyecto), con los
eventos `whatsapp.message.received` y `whatsapp.message.sent`. El secreto que
da Kapso va en `KAPSO_WEBHOOK_SECRET`.

## Estado

La versión con agente y herramientas funcionó de punta a punta el 18/09: un
mensaje de WhatsApp terminó siendo un turno real en Google Calendar, y una
cancelación lo borró y avisó al dueño.

Esta versión (una llamada por tanda) pasa `probar_puente.py` completo, pero
**todavía no se corrió `probar_prompt.py` contra el modelo**: se terminó el
crédito de la API. Hay que hacerlo antes de usarla, y de paso comparar con
Haiku (cuesta la mitad; su caché pide un prompt de al menos 4.096 tokens y este
anda cerca, así que puede que no se cachee).

Pendiente antes de ponerlo en producción:

- Correr `probar_prompt.py` con crédito y comparar modelos.
- Conectar el número real del lavadero.
- Aviso al dueño por WhatsApp: hoy sale por consola; falta la plantilla aprobada
  por Meta, y tiene que ir a otro número del dueño (no al del negocio).
- Hosting 24/7 en vez de una máquina local con ngrok. Tiene que poder correr
  `ffmpeg` y `whisper-cli`, o cambiar la transcripción por un servicio.
- Si la ubicación para un retiro llega después de agendado el turno, hoy no se
  agrega a la descripción.
- El código busca `google-credenciales.json` en minúscula. En macOS da igual,
  pero en un hosting Linux hay que respetar el nombre exacto.
- Las duraciones de los servicios en `prompt_agente.md` son estimaciones del
  rubro, no datos del lavadero. Confirmarlas con el dueño.
- El horario (lunes a viernes 8:30 a 18:00, sábados 9:00 a 18:30) sale de la
  página; en el Instagram hay un posteo que dice 9:30. Confirmarlo.

## Probado como QA

Encontré dos errores del agente probándolo con conversaciones reales:

1. **Atribución equivocada del nombre.** Cuando el cliente saludaba con "Hola
   Facu", el agente tomaba "Facu" como el nombre del cliente, cuando en realidad
   era el del dueño del lavadero.
2. **Servicio inventado.** Anotaba "lavado completo" en turnos donde nadie había
   dicho qué servicio era. Una alucinación chica, pero el dueño podía leerla como
   un pedido real del cliente.

Los dos se corrigen en el prompt, y los dos aparecieron recién al probar
conversaciones completas, no casos sueltos.

`probar_prompt.py` los deja como casos fijos. Antes de confiar en un caso, lo
corrí también contra el prompt anterior, para ver que ahí fallara:

- El servicio inventado y las duraciones sí fallan con el prompt anterior
  ("Lavado - Martín", 90 minutos para un cerámico) y pasan con el nuevo.
- El error del nombre ya no se reproduce con el modelo actual, ni siquiera con
  el prompt anterior. La regla queda en el prompt como protección, pero la
  prueba no puede demostrar que sea ella la que lo evita.

### Con charlas reales del lavadero (29/09)

Diez charlas y siete audios del WhatsApp del dueño mostraron cosas que no
estaban en el prompt: casi todos lo saludan "Hola Facu" (la trampa del nombre
es real), el dueño contesta mucho por audio, muchos acuerdos son "para el
sábado a la mañana" sin hora, el dueño también cancela ("no abrí por el
clima") y hay retiros a domicilio. De ahí salieron nueve casos más, con
nombres y teléfonos cambiados.

Estado: los 13 casos pasan salvo uno que falla de a ratos, y no siempre el
mismo: a veces supone el servicio ("Lavado de camioneta") cuando nadie lo dijo.
Pasó en 1 de 13 casos en cada una de las dos últimas corridas completas.
