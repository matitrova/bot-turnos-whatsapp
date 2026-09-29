# Bot de turnos por WhatsApp

Un agente de IA que agenda solo los turnos de un lavadero de autos. Es pasivo:
nunca le contesta al cliente. Lee la conversación de WhatsApp entre el cliente y
el dueño, que sigue atendiendo desde su celular como siempre, y cuando los dos
acuerdan un día y una hora, crea, mueve o cancela el turno en Google Calendar.
Nadie carga nada a mano.

## Cómo funciona

```
WhatsApp  →  Kapso  →  puente (Flask)  →  agente de IA  →  Google Calendar
                            ↑                    │
                            └──── herramientas ──┘
```

El **puente** (`puente.py`) es el centro de todo. Recibe el webhook de Kapso con
el mensaje de WhatsApp, se lo pasa al agente como una línea de conversación, y
cuando el agente pide usar una herramienta, **la ejecuta el puente, no el
agente**.

Esa separación es deliberada y es la decisión de diseño más importante del
proyecto.

## La seguridad no está en el prompt

Un agente hace lo que el prompt le pide, salvo cuando no. Si la única defensa
contra "cancelame el turno de Juan" fuera una instrucción escrita en el prompt,
alcanzaría con insistir para saltearla.

Acá no depende del prompt:

- Cada turno guarda el teléfono de su dueño en `extendedProperties.private`.
- `buscar_turnos` consulta el calendario filtrando por ese teléfono, así que el
  agente **solo ve los turnos de quien está escribiendo**. Los demás no existen
  para él.
- `mover_turno` y `cancelar_turno` verifican, antes de tocar nada, que el evento
  pertenezca a ese teléfono. Si no, devuelven un error y no hacen nada.

El agente no puede saltear el control porque el control vive en el código de las
herramientas, no en el texto que el agente lee.

## Herramientas disponibles para el agente

| Herramienta | Qué hace |
|---|---|
| `buscar_turnos` | Lista los turnos futuros **de ese cliente** |
| `crear_turno` | Crea el evento, con el teléfono del cliente guardado |
| `mover_turno` | Reprograma, previa verificación de propiedad |
| `cancelar_turno` | Borra, previa verificación de propiedad |
| `avisar_al_dueno` | Notifica al dueño de una cancelación |

El agente consulta `buscar_turnos` antes de crear, para no duplicar.

## Otros detalles del puente

- **Deduplicación**: guarda los ids de mensaje ya procesados, porque los webhooks
  se reintentan.
- **Una sesión por teléfono**: cada cliente tiene su propio hilo de conversación
  con el agente.
- **Solo acepta mensajes de Kapso**: verifica la firma `X-Webhook-Signature`
  (HMAC-SHA256 del cuerpo con `KAPSO_WEBHOOK_SECRET`). Sin eso, cualquiera que
  conociera la URL podría crear o cancelar turnos con mensajes falsos.
- **En orden, de a uno**: los mensajes entran a una cola que atiende un solo
  hilo. Así un "dale" no se adelanta a un audio que todavía se está
  transcribiendo.
- **Responde rápido**: el webhook contesta 200 enseguida y la cola procesa
  después, para que Kapso no lo dé por caído.
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

- `puente.py` — webhook, herramientas y conversación con el agente.
- `configurar_herramientas.py` — declara las herramientas del agente.
- `probar_calendario.py` — prueba aislada de la conexión con Google Calendar.
- `Probar_Agente.py` — prueba aislada de la conversación con el agente.
- `prompt_agente.md` — el prompt del agente. Se edita acá, no en la plataforma.
- `actualizar_prompt.py` — sube `prompt_agente.md` al agente (crea una versión
  nueva solo si cambió).
- `probar_prompt.py` — corre conversaciones armadas contra `prompt_agente.md`
  con un calendario de mentira, antes de subirlo. No toca Google Calendar.
  `python probar_prompt.py lluvia` corre solo los casos cuyo nombre lo contiene.
- `probar_puente.py` — prueba el webhook (firma, duplicados, historial, audios,
  ubicaciones, lotes) sin Kapso, sin agente y sin Google Calendar.

No van al repo: `conversaciones-reales/` (charlas y audios de clientes, datos
personales) y `modelos/` (el modelo de Whisper, 466 MB).

## Cómo correrlo

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask anthropic python-dotenv requests google-api-python-client google-auth

brew install ffmpeg whisper-cpp   # para transcribir los audios del dueño
mkdir -p modelos && curl -L -o modelos/ggml-small.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin

cp .env.example .env        # completar con los valores reales
# y dejar el JSON de la cuenta de servicio de Google como google-credenciales.json

python puente.py            # escucha en el puerto 8000
ngrok http 8000             # para exponerlo a Kapso durante el desarrollo
```

El calendario del dueño tiene que estar compartido con el email de la cuenta de
servicio.

En Kapso, el webhook se crea **en el número** (no en el proyecto), con los
eventos `whatsapp.message.received` y `whatsapp.message.sent`. El secreto que
da Kapso va en `KAPSO_WEBHOOK_SECRET`.

## Estado

Funciona de punta a punta: un mensaje de WhatsApp termina siendo un turno real
en Google Calendar, y una cancelación lo borra y avisa al dueño.

Pendiente antes de ponerlo en producción:

- Conectar el número real del lavadero (WhatsApp Business pide 7 días de uso
  previo para el modo coexistencia).
- Aviso al dueño por WhatsApp: hoy sale por consola; falta la plantilla aprobada
  por Meta. Ahora avisa también cuando se acuerda un día sin hora, así que esto
  pesa más.
- Hosting 24/7 en vez de una máquina local con ngrok.
- Persistir las sesiones: hoy viven en memoria y se pierden al reiniciar.
- El hosting tiene que poder correr `ffmpeg` y `whisper-cli`, o cambiar la
  transcripción de los audios del dueño por un servicio.
- Si la ubicación para un retiro llega después de agendado el turno, hoy no se
  agrega: no hay herramienta para editar la descripción.
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
