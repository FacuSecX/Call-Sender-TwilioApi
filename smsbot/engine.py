# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import asyncio
from collections import Counter
import logging
import secrets
import time

from .provider import PENDING, Result
from .store import now
from .simulation import valid_recipient
from . import voice
from .media import Media
from .assets import TwilioAssets, AssetsError

log = logging.getLogger("smsbot")


class Engine:
    def __init__(self, store, profiles, provider, settings):
        self.store, self.profiles, self.provider, self.settings = store, profiles, provider, settings
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.current_id: str | None = None
        self.rotation = secrets.randbelow(100000)
        self.start_index = secrets.randbelow
        self.notify = self.no_notify
        self.call_times = {}
        self.media=Media(getattr(settings,'data_dir',profiles.path.parent/'data')/'call-audio',getattr(settings,'audio_public_url',''),
                         TwilioAssets() if getattr(settings,'audio_backend','http')=='twilio' else None)

    async def no_notify(self, campaign_id):
        pass

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def order(self, profiles, preferred):
        profiles = list(profiles)
        if profiles:
            offset = self.rotation % len(profiles)
            profiles = profiles[offset:] + profiles[:offset]
            self.rotation += 1
        return sorted(profiles, key=lambda p: 0 if p["id"] == preferred else 1)

    def available(self, mode, channel="sms"):
        if mode == "simulation":
            return [{"id":"simulation-local", "label":"Simulador local", "mode":"simulation"}]
        profiles = self.profiles.all(enabled=True)
        return [p for p in profiles if voice.caller(p)] if channel!="sms" else profiles

    async def confirm(self, campaign_id: str) -> bool:
        if self.busy:
            raise ValueError("Ya hay un envío en ejecución. Podés detenerlo o esperar a que termine.")
        campaign = self.store.campaign(campaign_id)
        if not campaign:
            raise ValueError("El envío ya no existe.")
        channel=campaign.get("channel","sms")
        available = self.available(campaign["mode"],channel)
        if not available:
            raise ValueError("Agregá o activá una API"+(" con número de origen para llamadas" if channel!="sms" else "")+" antes de confirmar.")
        if channel!="sms" and campaign["mode"]=="test":
            raise ValueError("Usá el grupo simulado para probar llamadas sin costo.")
        if {p["mode"] for p in available} != {campaign["mode"]}:
            raise ValueError("Las APIs cambiaron de modo o mezclan prueba y producción. Prepará el mensaje otra vez.")
        recipients = self.store.recipients(campaign_id)
        for recipient in recipients:
            current = self.store.contact(recipient["contact_id"])
            if not current or not current["consent" if channel=="sms" else "voice_consent"] or current["phone"] != recipient["phone"]:
                raise ValueError("Un contacto cambió, fue eliminado o no tiene permiso de envío. Prepará el mensaje otra vez.")
            if not valid_recipient(current, campaign["mode"]):
                raise ValueError("La simulación y los envíos reales deben usar destinatarios separados.")
        if channel=='voice_audio':self.media.activate(campaign['audio_token'],campaign_id)
        if not self.store.confirm(campaign_id):
            return False
        self.rotation = self.start_index(len(available))
        self.current_id = campaign_id
        self.stop_event.clear()
        self.task = asyncio.create_task(self.run(campaign_id), name="sms-campaign")
        return True

    def stop(self, campaign_id: str):
        self.store.cancel(campaign_id)
        if self.current_id == campaign_id:
            self.stop_event.set()
        log.info("campaign_stop campaign=%s", campaign_id)

    def stopped(self, campaign_id):
        campaign = self.store.campaign(campaign_id)
        return self.stop_event.is_set() or not campaign or bool(campaign["stop"])

    async def delay(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def progress(self, campaign_id):
        try:
            await self.notify(campaign_id)
        except Exception as exc:
            log.warning("progress_update_failed category=%s", type(exc).__name__)

    async def send_recipient(self, campaign, recipient):
        cid = campaign["id"]
        channel=campaign.get("channel","sms")
        profiles = self.order([p for p in self.available(campaign["mode"],channel) if p["mode"]==campaign["mode"]], recipient["preferred_api"])
        if not profiles:
            self.store.update_recipient(recipient["id"], "failed", error="No hay APIs activas.")
            return
        last = Result("failed", detail="No hay una API disponible.")
        limited_accounts = set()
        for profile in profiles:
            account = profile.get("account_sid")
            if account in limited_accounts:
                continue
            for attempt in range(2):
                if self.stopped(cid):
                    self.store.update_recipient(recipient["id"], "cancelled")
                    return
                current = self.store.contact(recipient["contact_id"])
                if not current or not current["consent" if channel=="sms" else "voice_consent"] or current["phone"]!=recipient["phone"] or not valid_recipient(current, campaign["mode"]):
                    self.store.update_recipient(recipient["id"], "cancelled", error="Contacto eliminado o permiso de envío retirado.")
                    return
                if channel!="sms" and campaign["mode"]!="simulation":
                    if not await self.call_slot(cid,profile):
                        self.store.update_recipient(recipient["id"],"cancelled")
                        return
                self.store.update_recipient(recipient["id"], "submitting", api_id=profile["id"])
                self.store.attempt(recipient["id"], profile["id"], "submitting")
                await self.progress(cid)
                try:
                    if campaign["mode"] == "simulation":
                        if await self.delay(.8):
                            self.store.update_recipient(recipient["id"], "cancelled")
                            return
                        result = Result("simulated", detail="Simulación local: no se contactó a Twilio ni se envió un SMS.")
                    elif channel!="sms":
                        text=await self.media.play_url(campaign['audio_token'],cid,profile) if channel=='voice_audio' else campaign['body']
                        if self.stopped(cid):
                            self.store.update_recipient(recipient['id'],'cancelled');return
                        result = await voice.place(self.provider,profile,recipient["phone"],text,channel,voice_id=campaign.get("tts_voice",voice.DEFAULT_VOICE),audio_seconds=campaign.get('audio_seconds',0))
                    else:
                        result = await self.provider.send(profile, recipient["phone"], campaign["body"])
                except AssetsError as exc:
                    result = Result('failed',code='audio_prepare',detail=str(exc),alternate=True)
                except Exception as exc:
                    log.error("provider_unexpected category=%s campaign=%s", type(exc).__name__, cid)
                    result = Result("unknown", detail="Error inesperado durante el envío. No se repite para evitar duplicados.", ambiguous=True)
                self.store.attempt(recipient["id"], profile["id"], result.status, result.code)
                if result.code=="21610":
                    self.store.execute("UPDATE contacts SET consent=0 WHERE phone=?",(recipient["phone"],))
                log.info("sms_result campaign=%s recipient=%s api=%s status=%s code=%s", cid, recipient["phone"], profile["id"], result.status, result.code)
                self.store.update_recipient(recipient["id"], result.status, api_id=profile["id"], sid=result.sid, error=result.detail)
                await self.progress(cid)
                if channel!="sms" and (result.ambiguous or result.code=="10004"):
                    self.stop(cid)
                if result.sid or result.ambiguous or result.status != "failed":
                    return
                last = result
                if not result.retryable:
                    break
                if attempt == 0:
                    log.info("sms_retry_wait campaign=%s recipient=%s seconds=%s", cid, recipient["phone"], self.settings.retry_delay)
                    if await self.delay(self.settings.retry_delay):
                        self.store.update_recipient(recipient["id"], "cancelled")
                        return
            if not last.alternate:
                return
            if last.account_limited and account:
                limited_accounts.add(account)
        self.store.update_recipient(recipient["id"], "failed", error="Se agotaron las APIs disponibles. " + last.detail)

    async def call_slot(self,campaign_id,profile):
        limit=getattr(self.settings,"call_concurrency",1)
        while not self.stopped(campaign_id):
            active=self.store.one("SELECT COUNT(*) n FROM recipients r JOIN campaigns c ON c.id=r.campaign_id WHERE c.channel!='sms' AND c.mode!='simulation' AND r.status IN ('queued','ringing','in-progress')")["n"]
            if active<limit:
                interval=getattr(self.settings,"call_interval",1.1)
                wait=interval-(time.monotonic()-self.call_times.get(profile["account_sid"],0))
                if wait>0 and await self.delay(wait):return False
                self.call_times[profile["account_sid"]]=time.monotonic()
                return not self.stopped(campaign_id)
            await self.receipts_once()
            if await self.delay(5):return False
        return False

    async def run(self, campaign_id):
        self.store.execute("UPDATE campaigns SET status='running' WHERE id=?", (campaign_id,))
        log.info("campaign_start campaign=%s", campaign_id)
        try:
            campaign = self.store.campaign(campaign_id)
            recipients = self.store.recipients(campaign_id)
            for i, recipient in enumerate(recipients):
                if self.stopped(campaign_id):
                    break
                if recipient["status"] != "pending":
                    continue
                await self.send_recipient(campaign, recipient)
                if i + 1 < len(recipients) and await self.delay(self.settings.send_interval):
                    break
            stopped = self.stopped(campaign_id)
            if stopped:
                self.store.execute("UPDATE recipients SET status='cancelled',updated=? WHERE campaign_id=? AND status='pending'", (now(),campaign_id))
            self.store.execute("UPDATE campaigns SET status=? WHERE id=?", ("stopped" if stopped else "completed",campaign_id))
        except asyncio.CancelledError:
            self.store.execute("UPDATE campaigns SET status='interrupted',stop=1 WHERE id=?", (campaign_id,))
            self.store.execute("UPDATE recipients SET status='cancelled',error='Campaña interrumpida.',updated=? WHERE campaign_id=? AND status='pending'",(now(),campaign_id))
            raise
        except Exception as exc:
            log.error("campaign_failed campaign=%s category=%s", campaign_id,type(exc).__name__)
            self.store.execute("UPDATE campaigns SET status='interrupted',stop=1 WHERE id=?", (campaign_id,))
            self.store.execute("UPDATE recipients SET status='cancelled',error='Campaña interrumpida.',updated=? WHERE campaign_id=? AND status='pending'",(now(),campaign_id))
        finally:
            await self.cleanup_audio()
            await self.progress(campaign_id)
            self.current_id = None
            log.info("campaign_finished campaign=%s", campaign_id)

    async def receipts_once(self):
        rows = self.store.rows("SELECT * FROM recipients WHERE status IN ('accepted','scheduled','queued','sending','sent','ringing','in-progress') AND message_sid IS NOT NULL ORDER BY updated LIMIT 30")
        changed = set()
        for row in rows:
            profile = self.profiles.get(row["api_id"])
            if not profile or profile["mode"] == "test":
                continue
            result = (await voice.receipt(self.provider,profile,row["message_sid"]) if row["message_sid"].startswith("CA")
                      else await self.provider.receipt(profile, row["message_sid"]))
            if result:
                self.store.update_recipient(row["id"],result.status,api_id=profile["id"],sid=result.sid,error=result.detail)
                if result.status != row["status"] or result.detail != row["error"]:
                    changed.add(row["campaign_id"])
                    log.info("sms_receipt campaign=%s recipient=%s status=%s code=%s", row["campaign_id"],row["phone"],result.status,result.code)
        for campaign_id in changed:
            await self.progress(campaign_id)
        await self.cleanup_audio()

    async def cleanup_audio(self):
        self.media.cleanup(self.store)
        if self.media.assets:
            try:await self.media.assets.cleanup(self.media,self.profiles)
            except Exception as exc:log.warning('audio_cleanup_pending category=%s',type(exc).__name__)

    def summary(self, campaign_id):
        rows = self.store.recipients(campaign_id)
        counts = Counter(r["status"] for r in rows)
        return {"total":len(rows),"accepted":sum(counts[s] for s in PENDING),"delivered":counts["delivered"],
                "failed":counts["failed"]+counts["undelivered"],"unknown":counts["unknown"],
                "cancelled":counts["cancelled"]+counts["canceled"],"simulated":counts["simulated"],
                "pending":counts["pending"]+counts["submitting"],"voice_completed":counts["completed"],
                "voice_active":sum(counts[s] for s in voice.VOICE_PENDING),"no_answer":counts["no-answer"],"busy":counts["busy"]}

    async def close(self):
        if self.busy:
            self.stop_event.set()
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.media.assets:await self.media.assets.close()
