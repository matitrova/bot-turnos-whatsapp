import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(override=True)

credenciales = service_account.Credentials.from_service_account_file(
    "google-credenciales.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendario = build("calendar", "v3", credentials=credenciales)

inicio = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")) + timedelta(hours=1)
fin = inicio + timedelta(minutes=30)

evento = {
    "summary": "PRUEBA del bot",
    "start": {"dateTime": inicio.isoformat()},
    "end": {"dateTime": fin.isoformat()},
}

creado = calendario.events().insert(calendarId=os.environ["CALENDAR_ID"], body=evento).execute()
print("Evento creado:", creado["htmlLink"])