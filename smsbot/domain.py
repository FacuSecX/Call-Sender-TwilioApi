# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
import unicodedata

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType


def normalized(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def name(value: str) -> str:
    value = " ".join(value.split())
    if not 1 <= len(value) <= 60 or not any(c.isalpha() for c in value):
        raise ValueError("El nombre debe contener letras y tener hasta 60 caracteres.")
    if any(not (c.isalnum() or c in " ._-'’") for c in value):
        raise ValueError("Usá letras, espacios o números; no se admiten símbolos de control.")
    return value


def phone(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\+?\d{7,15}", value):
        raise ValueError("Ingresá solo dígitos, con código de país; el signo + es opcional.")
    try:
        parsed = phonenumbers.parse("+" + value.lstrip("+"), None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("El número no tiene un formato internacional válido.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("El número no pertenece a un rango telefónico válido. Revisá país y característica.")
    if phonenumbers.number_type(parsed) not in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        raise ValueError("Ingresá un celular. No se admiten fijos, servicios premium ni números especiales.")
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM_EXT = set("\f^{}\\[~]|€")


@dataclass(frozen=True)
class MessageSize:
    encoding: str
    units: int
    segments: int


def message_size(text: str) -> MessageSize:
    gsm = all(c in GSM or c in GSM_EXT for c in text)
    units = sum(2 if c in GSM_EXT else 1 for c in text) if gsm else len(text.encode("utf-16-be")) // 2
    single, multi = (160, 153) if gsm else (70, 67)
    return MessageSize("GSM-7" if gsm else "Unicode", units, 1 if units <= single else math.ceil(units / multi))


def body(value: str, max_segments: int = 6) -> str:
    value = value.strip()
    if not value or len(value) > 1600 or any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise ValueError("El mensaje debe tener texto, hasta 1600 caracteres y sin caracteres de control.")
    if message_size(value).segments > max_segments:
        raise ValueError(f"El texto supera el límite de {max_segments} segmentos. Acortalo para evitar gastos inesperados.")
    return value


def suggestions(text: str, contacts: list[dict], groups: list[dict]) -> list[dict]:
    query = normalized(text)
    ignored = {"enviar", "envia", "mandar", "manda", "sms", "mensaje", "mensajes", "grupo", "grupal", "a", "al", "para", "numero", "contacto"}
    words = [w for w in re.findall(r"[\w]+", query) if w not in ignored]
    if not words:
        return []
    needle = " ".join(words)
    result = []
    for kind, items in (("contact", contacts), ("group", groups)):
        for item in items:
            target = normalized(item["name"])
            score = max(SequenceMatcher(None, needle, target).ratio(), *(SequenceMatcher(None, needle, w).ratio() for w in target.split()))
            if needle in target:
                score = max(score, .94)
            if kind == "group" and "grupo" in query:
                score += .08
            if score >= .48:
                result.append({"kind": kind, "id": item["id"], "name": item["name"], "score": score})
    return sorted(result, key=lambda r: (-r["score"], normalized(r["name"])))[:16]
