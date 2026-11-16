# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import re
from xml.etree import ElementTree as ET
import httpx
import math
from urllib.parse import urlsplit

from .provider import Result, ERRORS

VOICE_PENDING = {"queued", "ringing", "in-progress"}
VOICE_FINAL = {"completed", "busy", "failed", "no-answer", "canceled"}
DEFAULT_VOICE = "female"
VOICES = {
    "female": {"label": "Femenina · Español", "voice": "woman", "language": "es"},
    "male": {"label": "Masculina · Español", "voice": "man", "language": "es-MX"},
}
VOICE_ERRORS = {
    "10004":"Se alcanzó el límite de llamadas simultáneas de la cuenta. No se intenta eludir con otra API.",
    "21210":"El número de origen de la llamada no está verificado o habilitado.",
    "21212":"El número de origen no es válido para llamadas.",
    "21215":"El destino no está habilitado en los permisos geográficos de llamadas.",
    "21219":"La cuenta de prueba requiere verificar este destinatario.",
    "21220":"La llamada no está permitida para esta cuenta o destino.",
}


def caller(profile):
    value = profile.get("voice_sender") or profile["sender"]
    return value if re.fullmatch(r"\+[1-9]\d{6,14}", value) else ""


def preset(voice_id):
    if not isinstance(voice_id,str) or voice_id not in VOICES:
        raise ValueError("Elegí una voz disponible con los botones.")
    return VOICES[voice_id]


def twiml(text, channel, voice_id=DEFAULT_VOICE):
    root = ET.Element("Response")
    if channel=="voice_text":
        if not isinstance(text,str) or not text.strip() or len(text)>600 or any(ord(c)<32 and c not in '\n\t' for c in text):
            raise ValueError("El texto de la llamada debe tener entre 1 y 600 caracteres.")
        selected=preset(voice_id)
        ET.SubElement(root,"Say",voice=selected["voice"],language=selected["language"]).text=text
    elif channel=='voice_audio':
        p=urlsplit(text)
        if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment or not re.fullmatch(r'/media/[0-9a-f]{64}\.(?:mp3|wav)',p.path):
            raise ValueError('El enlace del audio no es válido.')
        ET.SubElement(root,'Play',loop='1').text=text
    elif channel!="voice_ring":
        raise ValueError("Tipo de llamada inválido.")
    ET.SubElement(root,"Hangup")
    result=ET.tostring(root,encoding="unicode")
    if len(result)>4000:
        raise ValueError("El texto escapado supera el límite de la llamada.")
    return result


async def place(provider, profile, number, text, channel, voice_id=DEFAULT_VOICE,*,audio_seconds=0):
    sender=caller(profile)
    if not sender:
        return Result("failed",code="voice_sender",detail="Configurá un número de origen habilitado para llamadas en esta API.")
    if profile.get("mode")=="test":
        return Result("failed",code="voice_test",detail="Para probar llamadas sin costo usá el grupo de simulación local.")
    data={"To":number,"From":sender,"Twiml":twiml(text,channel,voice_id),"Timeout":"20","TimeLimit":"120"}
    if channel=='voice_audio':
        if not isinstance(audio_seconds,(int,float)) or not math.isfinite(audio_seconds) or not 0<audio_seconds<=2400:raise ValueError('Duración de audio inválida.')
        data['TimeLimit']=str(math.ceil(audio_seconds)+15)
    try:
        response=await provider.client.post(provider.base(profile)+"/Calls.json",data=data,auth=provider.auth(profile))
    except (httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout):
        return Result("failed",code="connection",detail="No se pudo conectar con Twilio.",retryable=True,alternate=True)
    except httpx.RequestError:
        return Result("unknown",code="uncertain",detail="Se perdió la respuesta de la llamada. No se repite para evitar llamar dos veces.",ambiguous=True)
    try:
        payload=response.json()
        if not isinstance(payload,dict): raise ValueError()
    except ValueError:
        return Result("unknown",code="invalid_response",detail="Respuesta de llamada no verificable. Revisá la consola; no se repite.",ambiguous=True)
    sid=payload.get("sid")
    if response.is_success and isinstance(sid,str) and re.fullmatch(r"CA[0-9a-fA-F]{32}",sid):
        status=payload.get("status","queued")
        return Result(status if status in VOICE_PENDING|VOICE_FINAL else "queued",sid=sid)
    code=str(payload.get("code") or response.status_code)
    if response.status_code>=500 or response.is_success:
        return Result("unknown",code=code,detail="No se confirmó si la llamada fue aceptada. No se repite.",ambiguous=True)
    retry=response.status_code==429 or code=="20429"
    return Result("failed",code=code,detail=VOICE_ERRORS.get(code,ERRORS.get(code,f"Twilio rechazó la llamada ({code}).")),
                  retryable=retry,alternate=code in {"20003","21210"} or retry,account_limited=retry)


async def receipt(provider,profile,sid):
    if not re.fullmatch(r"CA[0-9a-fA-F]{32}",sid): return None
    try:
        response=await provider.client.get(provider.base(profile)+f"/Calls/{sid}.json",auth=provider.auth(profile))
        payload=response.json()
        if not response.is_success or not isinstance(payload,dict) or payload.get("status") not in VOICE_PENDING|VOICE_FINAL:return None
        status=payload["status"]
        detail={"busy":"El destinatario estaba ocupado.","no-answer":"El destinatario no atendió.","failed":"Twilio no pudo completar la llamada."}.get(status,"")
        return Result(status,sid=sid,detail=detail)
    except (httpx.RequestError,ValueError,AttributeError):return None
