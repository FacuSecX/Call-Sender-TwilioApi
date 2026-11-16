# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

from dotenv import dotenv_values

from .domain import name
from .media import public_base


@dataclass(frozen=True)
class Settings:
    env_path: Path
    token: str
    chat_id: int
    user_ids: frozenset[int]
    data_dir: Path
    log_file: Path
    send_interval: float = 1.0
    retry_delay: float = 5.0
    receipt_interval: float = 15.0
    max_segments: int = 6
    max_recipients: int = 500
    call_interval: float = 1.1
    call_concurrency: int = 1
    audio_public_url: str = ''
    audio_bind_host: str = '127.0.0.1'
    audio_bind_port: int = 8787
    audio_backend: str = 'http'

    @classmethod
    def load(cls, env_path: Path) -> "Settings":
        env_path = env_path.resolve()
        data = {**dotenv_values(env_path, interpolate=False)}
        token = data.get("TELEGRAM_BOT_TOKEN", "") or ""
        if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{25,}", token):
            raise ValueError("Configurá TELEGRAM_BOT_TOKEN en .env.")
        try:
            chat_id = int(data.get("TELEGRAM_CHAT_ID") or "0")
            ids = frozenset(int(i.strip()) for i in (data.get("TELEGRAM_USER_IDS") or "").split(",") if i.strip())
        except ValueError as exc:
            raise ValueError("Chat ID o usuarios autorizados inválidos.") from exc
        if not chat_id or (chat_id < 0 and not ids):
            raise ValueError("Configurá Chat ID; en grupos también necesitás TELEGRAM_USER_IDS.")
        if chat_id > 0:
            ids = frozenset({chat_id})
        base = env_path.parent
        audio_host=data.get('AUDIO_BIND_HOST') or '127.0.0.1'
        audio_port=int(data.get('AUDIO_BIND_PORT') or 8787)
        audio_backend=data.get('AUDIO_BACKEND') or 'http'
        if audio_backend not in {'http','twilio'}:raise ValueError('El proveedor de audios no es válido.')
        if audio_host not in {'127.0.0.1','0.0.0.0'} or not 1024<=audio_port<=65535:
            raise ValueError('Configuración del servidor de audios inválida.')
        return cls(env_path, token, chat_id, ids, (base / (data.get("DATA_DIR") or "data")).resolve(),
                   (base / (data.get("LOG_FILE") or "bot.log")).resolve(),
                   max(.2, float(data.get("SEND_INTERVAL_SECONDS") or 1)),
                   max(5, float(data.get("RETRY_DELAY_SECONDS") or 5)),
                   max(5, float(data.get("RECEIPT_INTERVAL_SECONDS") or 15)),
                   min(10, max(1, int(data.get("MAX_MESSAGE_SEGMENTS") or 6))),
                   min(5000, max(1, int(data.get("MAX_CAMPAIGN_RECIPIENTS") or 500))),
                   max(1.1, float(data.get("CALL_INTERVAL_SECONDS") or 1.1)),
                   min(20, max(1, int(data.get("MAX_CONCURRENT_CALLS") or 1))),
                   public_base(data.get('AUDIO_PUBLIC_URL') or ''),audio_host,audio_port,audio_backend)


def atomic_env(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            for key, value in values.items():
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    raise ValueError("Clave de configuración inválida.")
                if value is not None:
                    out.write(key + "=" + json.dumps(str(value), ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_profile(profile: dict) -> dict:
    label = name(profile.get("label", ""))
    sid = profile.get("account_sid", "").strip()
    username = profile.get("username", sid).strip()
    secret = profile.get("secret", "").strip()
    sender = profile.get("sender", "").strip()
    mode = profile.get("mode", "live")
    voice_sender = profile.get("voice_sender", "").strip()
    if voice_sender and not re.fullmatch(r"\+[1-9]\d{6,14}", voice_sender):
        raise ValueError("El origen de llamadas debe ser un número Twilio o verificado con formato +país.")
    if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid) or not re.fullmatch(r"(?:AC|SK)[0-9a-fA-F]{32}", username):
        raise ValueError("Account SID debe empezar con AC; la credencial puede ser ese AC o una API Key SK.")
    if username.startswith("AC") and username != sid:
        raise ValueError("El usuario de autenticación AC debe coincidir con el Account SID.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", secret):
        raise ValueError("Auth Token o API Secret inválido.")
    if not (re.fullmatch(r"MG[0-9a-fA-F]{32}", sender) or re.fullmatch(r"\+[1-9]\d{6,14}", sender) or
            (re.fullmatch(r"[A-Za-z0-9 ]{1,11}", sender) and any(c.isalpha() for c in sender))):
        raise ValueError("Remitente: número Twilio +país, nombre alfanumérico de hasta 11 caracteres o Messaging Service MG.")
    if mode not in {"live", "test"}:
        raise ValueError("El modo debe ser live o test.")
    if mode == "test" and not re.fullmatch(r"\+[1-9]\d{6,14}", sender):
        raise ValueError("El modo test requiere configurar un remitente numérico autorizado por Twilio.")
    if not isinstance(profile.get("enabled",True),bool):
        raise ValueError("El estado de la API debe ser true o false.")
    profile_id = profile.get("id") or secrets.token_hex(6)
    if not isinstance(profile_id,str) or not re.fullmatch(r"[0-9a-f]{12}",profile_id):
        raise ValueError("Identificador de API inválido.")
    return {"id": profile_id, "label": label, "account_sid": sid,
            "username": username, "secret": secret, "sender": sender, "mode": mode,
            "enabled": bool(profile.get("enabled", True)), "voice_sender":voice_sender}


class Profiles:
    def __init__(self, path: Path):
        self.path = path

    def all(self, enabled: bool = False) -> list[dict]:
        raw = dotenv_values(self.path, interpolate=False).get("TWILIO_PROFILES_JSON") or "[]"
        try:
            profiles = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("TWILIO_PROFILES_JSON no contiene una lista válida.") from exc
        if not isinstance(profiles, list):
            raise ValueError("TWILIO_PROFILES_JSON debe ser una lista.")
        validated = [validate_profile(p) for p in profiles]
        ids = [p["id"] for p in validated]
        if len(set(ids)) != len(ids):
            raise ValueError("Hay identificadores de API repetidos.")
        return [p for p in validated if p["enabled"]] if enabled else validated

    def save(self, profiles: list[dict]) -> None:
        profiles = [validate_profile(p) for p in profiles]
        if len(profiles) > 20:
            raise ValueError("El máximo es de 20 conexiones API.")
        data = dict(dotenv_values(self.path, interpolate=False))
        data["TWILIO_PROFILES_JSON"] = json.dumps(profiles, ensure_ascii=False, separators=(",", ":"))
        atomic_env(self.path, data)

    def add(self, profile: dict) -> dict:
        profile = validate_profile(profile)
        profiles = self.all()
        if any(p["label"].casefold() == profile["label"].casefold() for p in profiles):
            raise ValueError("Ya existe una API con ese nombre.")
        profiles.append(profile)
        self.save(profiles)
        return profile

    def get(self, profile_id: str) -> dict | None:
        return next((p for p in self.all() if p["id"] == profile_id), None)

    def delete(self, profile_id: str) -> None:
        self.save([p for p in self.all() if p["id"] != profile_id])

    def toggle(self, profile_id: str) -> None:
        profiles = self.all()
        for profile in profiles:
            if profile["id"] == profile_id:
                profile["enabled"] = not profile["enabled"]
        self.save(profiles)
