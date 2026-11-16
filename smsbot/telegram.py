# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import asyncio
from html import escape, unescape
import re
import time
import os

import httpx


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        self.code = code
        self.description = description
        super().__init__("Telegram no pudo completar la operación.")


class Telegram:
    def __init__(self, token: str, client=None):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.file_base = "https://api.telegram.org/file/bot" + token + "/"
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), trust_env=False)
        self.owns_client = client is None

    async def call(self, method: str, **payload):
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("Método de Telegram inválido.")
        for attempt in range(3):
            try:
                response = await self.client.post(self.base + method, json=payload)
                data = response.json()
            except (httpx.RequestError, ValueError) as exc:
                if method == "getUpdates" or attempt == 2:
                    raise TelegramError(0, "Error de conexión con Telegram.") from None
                await asyncio.sleep(1 + attempt)
                continue
            if data.get("ok"):
                return data.get("result")
            if data.get("error_code") == 429 and attempt < 2:
                await asyncio.sleep(min(60, max(1, data.get("parameters",{}).get("retry_after",2))))
                continue
            raise TelegramError(data.get("error_code",response.status_code), str(data.get("description",""))[:200])

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def download(self,file_id,target,max_bytes):
        if not isinstance(file_id,str) or not 1<=len(file_id)<=1024:raise ValueError('El archivo de Telegram no es válido.')
        info=await self.call('getFile',file_id=file_id)
        path=info.get('file_path','')
        if not isinstance(path,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,250}',path) or path.startswith('/') or any(p in ('','..','.') for p in path.split('/')):
            raise ValueError('Telegram devolvió una ruta de archivo no válida.')
        if info.get('file_size') is not None and (not isinstance(info['file_size'],int) or not 0<info['file_size']<=max_bytes):raise ValueError('El archivo supera el límite de 20 MB.')
        try:
            async with self.client.stream('GET',self.file_base+path,follow_redirects=False) as response:
                if response.status_code!=200:raise ValueError('No se pudo descargar el archivo de Telegram.')
                with open(target,'xb') as out:
                    os.chmod(target,0o600);size=0
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>max_bytes:raise ValueError('El archivo supera el límite de 20 MB.')
                        out.write(chunk)
                if not size:raise ValueError('El archivo está vacío.')
        except httpx.RequestError:
            raise ValueError('No se pudo descargar el MP3. Volvé a adjuntarlo.') from None


def button(text, callback, style=None):
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("El botón excede el tamaño permitido.")
    result = {"text":text,"callback_data":callback}
    if style:
        result["style"] = style
    return result


class Screen:
    def __init__(self, telegram, store, chat_id):
        self.telegram, self.store, self.chat_id = telegram, store, chat_id
        self.lock = asyncio.Lock()
        self.last_render = None
        self.last_time = 0.

    @property
    def message_id(self):
        return self.store.state("menu_message_id")

    async def delete(self, message_id):
        try:
            await self.telegram.call("deleteMessage",chat_id=self.chat_id,message_id=message_id)
            self.store.execute("DELETE FROM chat_messages WHERE chat_id=? AND message_id=?", (self.chat_id,message_id))
            return True
        except TelegramError:
            return False

    async def show(self, text, rows, view="main", expected_view=None):
        visible = unescape(re.sub(r"<[^>]+>","",text))
        if len(visible)>4000:
            text = escape(visible[:3800]+"\n… Vista resumida. Abrí el detalle para ver más.")
        keyboard = {"inline_keyboard": rows}
        async with self.lock:
            if expected_view is not None and self.store.state("view")!=expected_view:
                return
            self.store.set_state("view",view)
            render = (text,keyboard)
            if render == self.last_render:
                return
            remaining = .8 - (time.monotonic() - self.last_time)
            if remaining > 0:
                await asyncio.sleep(remaining)
            existing = self.message_id
            if existing:
                try:
                    await self.telegram.call("editMessageText",chat_id=self.chat_id,message_id=existing,text=text,parse_mode="HTML",reply_markup=keyboard)
                    self.last_render, self.last_time = render, time.monotonic()
                    return
                except TelegramError as exc:
                    if "message is not modified" in exc.description.lower():
                        self.last_render = render
                        return
                    if exc.code not in (400,404):
                        raise
                    await self.delete(existing)
            result = await self.telegram.call("sendMessage",chat_id=self.chat_id,text=text,parse_mode="HTML",reply_markup=keyboard)
            self.store.set_state("menu_message_id", result["message_id"])
            self.store.track(self.chat_id,result["message_id"])
            self.last_render, self.last_time = render,time.monotonic()

    async def clear(self):
        rows = self.store.rows("SELECT message_id FROM chat_messages WHERE chat_id=? ORDER BY message_id DESC",(self.chat_id,))
        semaphore = asyncio.Semaphore(4)
        async def delete(row):
            async with semaphore:
                return await self.delete(row["message_id"])
        results = await asyncio.gather(*(delete(r) for r in rows))
        self.store.set_state("menu_message_id", None)
        self.last_render = None
        return sum(results), len(rows)-sum(results)
