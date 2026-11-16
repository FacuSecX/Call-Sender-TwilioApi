# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

import httpx


@dataclass
class Result:
    status: str
    sid: str | None = None
    code: str = ""
    detail: str = ""
    retryable: bool = False
    alternate: bool = False
    ambiguous: bool = False
    account_limited: bool = False


ERRORS = {
    "20003": "Twilio rechazó las credenciales o sus permisos.",
    "20006": "La cuenta está suspendida; revisala en Twilio.",
    "20429": "Twilio rechazó la solicitud por exceso de consultas.",
    "21211": "El número de destino no es válido para Twilio.",
    "21408": "El país de destino no está habilitado en los permisos de Twilio.",
    "21606": "El remitente no está habilitado para enviar SMS en esta cuenta.",
    "21608": "La cuenta de prueba solo puede enviar a destinatarios verificados.",
    "21610": "El destinatario pidió la baja. No se probarán otras APIs.",
    "21612": "La combinación de remitente y destino no está admitida.",
    "21614": "El destinatario no es un celular válido para SMS.",
    "21617": "El mensaje supera el tamaño permitido.",
    "21620": "El mensaje está vacío.",
    "30003": "El celular no está disponible.",
    "30005": "Twilio informa un destinatario desconocido.",
    "30006": "El destino no admite SMS.",
    "30007": "Mensaje filtrado por el proveedor u operador. No se reenvía por otra API.",
    "30008": "El operador informó un error de entrega.",
}
PENDING = {"accepted", "scheduled", "queued", "sending", "sent"}


class Twilio:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(25, connect=7), follow_redirects=False, trust_env=False)
        self.owns_client = client is None

    @staticmethod
    def auth(profile: dict) -> httpx.BasicAuth:
        return httpx.BasicAuth(profile["username"], profile["secret"])

    @staticmethod
    def base(profile: dict) -> str:
        sid = profile["account_sid"]
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
            raise ValueError("Account SID inválido.")
        return f"https://api.twilio.com/2010-04-01/Accounts/{sid}"

    async def send(self, profile: dict, number: str, text: str) -> Result:
        if not re.fullmatch(r"\+[1-9]\d{6,14}", number):
            return Result("failed", code="invalid_recipient", detail="El destinatario no es un teléfono válido.")
        data = {"To": number, "Body": text}
        sender = profile["sender"]
        data["MessagingServiceSid" if sender.startswith("MG") else "From"] = sender
        if profile.get("mode") == "test" and not re.fullmatch(r"\+[1-9]\d{6,14}", sender):
            return Result("failed", code="test_sender", detail="Configurá el remitente autorizado para las credenciales de prueba de Twilio.")
        try:
            response = await self.client.post(self.base(profile) + "/Messages.json", data=data, auth=self.auth(profile))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return Result("failed", code="connection", detail="No se pudo establecer la conexión con Twilio.", retryable=True, alternate=True)
        except httpx.RequestError:
            return Result("unknown", code="uncertain", detail="Se perdió la respuesta durante el envío. Podría haberse aceptado: no se reenvía automáticamente.", ambiguous=True)
        try:
            payload = response.json()
        except ValueError:
            return Result("unknown", code="invalid_response", detail="Twilio no devolvió una respuesta JSON verificable. No se reenvía para evitar duplicados.", ambiguous=True)
        if not isinstance(payload, dict):
            return Result("unknown", code="invalid_response", detail="La respuesta de Twilio no es verificable; revisá el historial del proveedor.", ambiguous=True)
        sid = payload.get("sid")
        if response.is_success and isinstance(sid, str) and re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", sid):
            status = payload.get("status", "accepted")
            if profile.get("mode") == "test":
                return Result("simulated", sid=sid, detail="Prueba oficial de Twilio: no se envió un SMS real.")
            if status not in PENDING | {"delivered", "failed", "undelivered", "canceled"}:
                status = "accepted"
            code = str(payload.get("error_code") or "")
            return Result(status, sid=sid, code=code, detail=ERRORS.get(code, ""))
        code = str(payload.get("code") or response.status_code)
        if response.status_code >= 500 or response.is_success:
            return Result("unknown", code=code, detail="No se pudo confirmar si Twilio aceptó el envío. No se reenvía automáticamente.", ambiguous=True)
        retryable = response.status_code == 429 or code == "20429"
        alternate = code in {"20003", "21606"} or retryable
        detail = ERRORS.get(code, f"Twilio rechazó la solicitud (código {code}). Revisá su consola.")
        return Result("failed", code=code, detail=detail, retryable=retryable, alternate=alternate, account_limited=retryable)

    async def receipt(self, profile: dict, sid: str) -> Result | None:
        if not re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", sid):
            return None
        try:
            response = await self.client.get(self.base(profile) + f"/Messages/{sid}.json", auth=self.auth(profile))
            if not response.is_success:
                return None
            payload = response.json()
            status = payload.get("status")
            if status not in PENDING | {"delivered", "failed", "undelivered", "canceled"}:
                return None
            code = str(payload.get("error_code") or "")
            return Result(status, sid=sid, code=code, detail=ERRORS.get(code, f"Error de entrega {code}" if code else ""))
        except (httpx.RequestError, ValueError, AttributeError):
            return None

    async def health(self, profile: dict) -> dict:
        if profile["mode"] == "test":
            return {"status": "test", "description": "Credencial de prueba: no permite consultar saldo real ni confirmar entrega."}
        try:
            response = await self.client.get(self.base(profile) + "/Balance.json", auth=self.auth(profile))
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Respuesta de saldo inválida.")
            if not response.is_success:
                code = str(payload.get("code") or response.status_code)
                detail = "La credencial no tiene permiso para consultar el saldo (70004)." if code == "70004" else ERRORS.get(code, f"No se pudo consultar el saldo ({code}).")
                return {"status": "error", "description": detail}
            amount, currency = payload.get("balance"), payload.get("currency")
            if amount is None or not Decimal(str(amount)).is_finite() or not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
                raise ValueError("Respuesta de saldo incompleta.")
            return {"status": "ok", "balance": str(amount), "currency": currency.upper(), "description": "Consulta de saldo correcta."}
        except (httpx.RequestError, ValueError, AttributeError, InvalidOperation):
            return {"status": "error", "description": "No se pudo obtener un saldo verificable de Twilio. Las credenciales no se muestran."}

    async def prices(self, profile: dict, country: str) -> dict | None:
        if not re.fullmatch(r"[A-Z]{2}", country) or profile["mode"] == "test":
            return None
        try:
            response = await self.client.get(f"https://pricing.twilio.com/v1/Messaging/Countries/{country}", auth=self.auth(profile))
            if not response.is_success:
                return None
            payload = response.json()
            prices = [Decimal(p["current_price"]) for carrier in payload.get("outbound_sms_prices", []) for p in carrier.get("prices", []) if Decimal(p["current_price"]) > 0]
            if not prices:
                return None
            return {"min": str(min(prices)), "max": str(max(prices)), "currency": payload.get("price_unit", "USD")}
        except (httpx.RequestError, ValueError, KeyError, TypeError, InvalidOperation):
            return None

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
