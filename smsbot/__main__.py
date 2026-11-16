# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import sys

from .bot import Bot
from .config import Profiles, Settings
from .engine import Engine
from .provider import Twilio
from .store import Store
from .simulation import prepare_demo
from .health import mark_ready
from .telegram import Telegram, TelegramError
from .media import AudioHTTPServer


class Redact(logging.Filter):
    def filter(self,record):
        text = record.getMessage()
        text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b","[token omitido]",text)
        text = re.sub(r"(?i)(authorization|auth_token|api_secret|password|secret)[=: ]+[^\s,]+",r"\1=[omitido]",text)
        record.msg,record.args = text,()
        return True


def logging_setup(path):
    handler = RotatingFileHandler(path,maxBytes=5*1024*1024,backupCount=5,encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(Redact())
    logger = logging.getLogger("smsbot")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    path.chmod(0o600)
    return logger


class InstanceLock:
    def __init__(self,path):
        self.handle = open(path,"a+b")
        try:
            if os.name=="nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1): self.handle.write(b"0"); self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValueError("Ya hay otra instancia del bot usando estos datos.") from None

    def close(self):
        self.handle.close()


async def run(settings):
    settings.data_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    settings.data_dir.chmod(0o700)
    lock = InstanceLock(settings.data_dir/"bot.lock")
    log = logging_setup(settings.log_file)
    store = Store(settings.data_dir/"smsbot.sqlite3")
    profiles = Profiles(settings.env_path)
    profiles.all()
    provider,telegram = Twilio(),Telegram(settings.token)
    engine = Engine(store,profiles,provider,settings)
    bot = Bot(settings,store,profiles,engine,telegram)
    audio_server=None
    if engine.media.base and not engine.media.assets:
        try:
            audio_server=AudioHTTPServer(engine.media,settings.audio_bind_host,settings.audio_bind_port)
            audio_server.start()
        except OSError:
            log.warning('audio_server_unavailable');engine.media.base='';audio_server=None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name!="nt":
        for sig in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(sig,stop.set)
    store.recover()
    async def receipts():
        while not stop.is_set():
            try:
                await engine.receipts_once()
                request = store.state("simulation_request")
                if request and not engine.busy:
                    store.set_state("simulation_request", None)
                    campaign = store.campaign(request)
                    if campaign and campaign["mode"]=="simulation" and campaign["status"]=="draft":
                        store.set_state("active_draft", request)
                        await bot.confirmation(request)
                        try:
                            await asyncio.wait_for(stop.wait(), 6)
                        except asyncio.TimeoutError:
                            if await engine.confirm(request):
                                store.set_state("active_draft", None)
                                await bot.campaign_view(request)
            except Exception as exc:
                log.warning("receipt_poll_failed category=%s",type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(),settings.receipt_interval)
            except asyncio.TimeoutError:
                pass
    receipt_task = None
    backup_task = None
    async def backup_watch():
        while not stop.is_set():
            try:await bot.backup_watch()
            except Exception as exc:log.warning('backup_status_failed category=%s',type(exc).__name__)
            try:await asyncio.wait_for(stop.wait(),3)
            except asyncio.TimeoutError:pass
    try:
        me = await telegram.call("getMe")
        webhook = await telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("El bot ya tiene un webhook configurado. No se modifica automáticamente.")
        try:
            await bot.register_commands()
            await bot.main()
        except TelegramError as exc:
            log.warning("telegram_initial_menu_pending code=%s",exc.code)
        log.info("bot_started version=1.0.0 username=%s",me.get("username",""))
        mark_ready(settings,store)
        receipt_task = asyncio.create_task(receipts())
        backup_task = asyncio.create_task(backup_watch())
        offset = store.state("telegram_offset",0)
        while not stop.is_set():
            try:
                updates = await telegram.call("getUpdates",offset=offset,timeout=25,allowed_updates=["message","callback_query"])
                for update in updates:
                    try:
                        await bot.handle(update)
                    except TelegramError as exc:
                        log.warning("telegram_operation_failed code=%s detail=%s",exc.code,exc.description)
                    except Exception as exc:
                        log.error("update_failed category=%s",type(exc).__name__)
                    offset = max(offset,update["update_id"]+1)
                    store.set_state("telegram_offset",offset)
            except TelegramError as exc:
                if exc.code in (401,409):
                    raise ValueError("Telegram rechazó el token o detectó otra instancia usando el mismo bot.") from None
                log.warning("telegram_poll_retry code=%s",exc.code)
                try:
                    await asyncio.wait_for(stop.wait(),3)
                except asyncio.TimeoutError:
                    pass
    finally:
        stop.set()
        if receipt_task:
            receipt_task.cancel()
            await asyncio.gather(receipt_task,return_exceptions=True)
        if backup_task:
            backup_task.cancel()
            await asyncio.gather(backup_task,return_exceptions=True)
        await bot.close()
        if audio_server:await asyncio.to_thread(audio_server.close)
        await engine.close()
        await provider.close()
        await telegram.close()
        store.close()
        lock.close()
        log.info("bot_stopped")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Bot SMS API Twilio")
    parser.add_argument("--env",type=Path,default=Path(".env"))
    parser.add_argument("--check-config",action="store_true")
    parser.add_argument("--check-telegram",action="store_true")
    parser.add_argument("--demo-group",action="store_true",help="Mostrar una simulación grupal sin enviar SMS en el bot activo")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.env)
        profiles = Profiles(settings.env_path).all()
        if os.name!="nt" and settings.env_path.stat().st_mode & 0o077:
            raise ValueError("El archivo .env debe tener permisos 600.")
        if args.demo_group:
            store = Store(settings.data_dir/"smsbot.sqlite3")
            try:
                print(json.dumps({"simulation_queued":prepare_demo(store), "real_sms":0},ensure_ascii=False))
            finally:
                store.close()
        elif args.check_config:
            print(json.dumps({"config":"ok","apis":len(profiles)},ensure_ascii=False))
        elif args.check_telegram:
            async def check():
                telegram = Telegram(settings.token)
                try:
                    result = await telegram.call("getMe")
                    print(json.dumps({"telegram":"ok","username":result.get("username")},ensure_ascii=False))
                finally:
                    await telegram.close()
            asyncio.run(check())
        else:
            asyncio.run(run(settings))
    except (ValueError,TelegramError) as exc:
        print(str(exc) if isinstance(exc,ValueError) else "No se pudo conectar con Telegram. Revisá el token y la conexión.",file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__=="__main__":
    raise SystemExit(main())
