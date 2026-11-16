# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import re

from dotenv import dotenv_values

from smsbot.config import Settings, atomic_env, validate_profile


def ask(prompt, default=""):
    value = input(prompt+(f" [{default}]" if default else "")+": ").strip()
    return value or default


def main():
    os.umask(0o077)
    path = Path(__file__).resolve().parent/".env"
    values = dict(dotenv_values(path,interpolate=False)) if path.exists() else {}
    if values and ask("Ya existe .env. ¿Conservar su configuración? s/n","s").lower()=="s":
        Settings.load(path)
        print("Configuración existente conservada.")
        return
    token = getpass.getpass("Token de BotFather (no se muestra): ").strip()
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{25,}",token):
        raise ValueError("Token de Telegram inválido.")
    chat_id = ask("Chat ID autorizado")
    if not re.fullmatch(r"-?\d{5,20}",chat_id):
        raise ValueError("Chat ID inválido.")
    user_ids = ask("IDs de usuarios autorizados en el grupo, separados por coma") if chat_id.startswith("-") else ""
    profiles = []
    while ask("¿Agregar una conexión Twilio ahora? s/n","n").lower()=="s":
        label = ask("Nombre de la conexión")
        sid = ask("Account SID (AC...)")
        username = ask("SID para autenticar: Account SID o API Key SK",sid)
        secret = getpass.getpass("Auth Token o API Secret (no se muestra): ").strip()
        sender = ask("Remitente habilitado, número Twilio o Messaging Service MG")
        mode = ask("Modo live o test (test requiere credenciales de prueba oficiales)","live")
        try:
            profile = validate_profile({"label":label,"account_sid":sid,"username":username,"secret":secret,"sender":sender,"mode":mode})
            profiles.append(profile)
            print("Conexión agregada. No se envió ningún SMS.")
        except ValueError as exc:
            print(str(exc)+" No se guardó esta conexión; podés agregarla después desde Telegram.")
    values.update(TELEGRAM_BOT_TOKEN=token,TELEGRAM_CHAT_ID=chat_id,TELEGRAM_USER_IDS=user_ids,TWILIO_PROFILES_JSON=json.dumps(profiles,ensure_ascii=False))
    atomic_env(path,values)
    Settings.load(path)
    print("Configuración guardada en .env con permisos privados.")


if __name__=="__main__":
    try:
        main()
    except (ValueError,EOFError,KeyboardInterrupt) as exc:
        print(str(exc) if isinstance(exc,ValueError) else "Configuración cancelada.")
        raise SystemExit(1)
