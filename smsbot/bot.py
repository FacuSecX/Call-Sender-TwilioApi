# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as h
import logging
import re
from pathlib import Path

from .config import validate_profile
from .domain import body, message_size, name, suggestions
from .provider import PENDING
from .telegram import Screen, TelegramError, button as b
from . import voice
from .backups import Backups, download_url

log = logging.getLogger("smsbot")
BACK = [[b("🏠 Menú principal","main","primary")]]
LABELS = {"pending":"Pendiente","submitting":"Enviando solicitud","accepted":"Aceptado","scheduled":"Programado en Twilio","queued":"En cola","sending":"Enviando","sent":"Entregado al operador","delivered":"Entregado al celular","undelivered":"No entregado","failed":"Rechazado / fallido","unknown":"Resultado incierto","cancelled":"Cancelado","canceled":"Cancelado","simulated":"Prueba sin envío real","ringing":"Sonando","in-progress":"Llamada atendida / en curso","completed":"Llamada completada","busy":"Ocupado","no-answer":"Sin respuesta"}


class Bot:
    def __init__(self, settings, store, profiles, engine, telegram):
        self.settings, self.store, self.profiles, self.engine, self.telegram = settings,store,profiles,engine,telegram
        self.screen = Screen(telegram,store,settings.chat_id)
        self.flow = None
        self.background = set()
        self.status_task = None
        self.backups = Backups(project=getattr(settings,'env_path',Path.cwd()/'.env').parent)
        self.backup_notice = None
        self.media=engine.media
        engine.notify = self.progress

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def authorized(self, update):
        source = update.get("callback_query") or update.get("message") or {}
        message = source.get("message",source)
        return message.get("chat",{}).get("id") == self.settings.chat_id and source.get("from",{}).get("id") in self.settings.user_ids

    async def register_commands(self):
        commands = [("start","Menú principal"),("enviar","Enviar SMS individual"),("grupal","Enviar SMS a un grupo"),("numeros","Gestionar números"),("agregar","Agregar un número"),("grupos","Gestionar grupos"),("favoritos","Números favoritos"),("apis","Gestionar conexiones Twilio"),("estado","Saldo y estado de las APIs"),("historial","Historial de envíos"),("detener","Detener el envío en curso"),("limpiar","Limpiar mensajes del bot"),("cancelar","Cancelar la operación actual"),("ayuda","Ayuda y funcionamiento")]
        commands += [("llamar","Llamar a un contacto"),("llamadas","Llamadas grupales"),("backups","Exportar o importar backup MEGA")]
        await self.telegram.call("setMyCommands",commands=[{"command":c,"description":d} for c,d in commands],scope={"type":"default"})
        await self.telegram.call("setChatMenuButton",menu_button={"type":"commands"})

    async def main(self, notice=""):
        self.flow = None
        active = "\n🟠 Hay un envío en ejecución; /detener frena los siguientes SMS." if self.engine.busy else ""
        await self.screen.show((h(notice)+"\n\n" if notice else "")+"<b>📨 Bot SMS · Twilio</b>\nElegí una opción. Cada envío requiere tu confirmación."+active,
            [[b("✉️ Enviar SMS","sendlist:0","success"),b("👥 Enviar SMS grupal","sendgroups:0","success")],
             [b("📞 Enviar llamada","calllist:0","success"),b("📞 Llamadas grupales","callgroups:0","success")],
             [b("📒 Números","contacts:0","primary"),b("⭐ Favoritos","favorites:0","primary")],
             [b("👥 Grupos","groups:0","primary"),b("🔌 APIs Twilio","apis","primary")],
             [b("📋 Historial","history:0"),b("🧹 Limpiar chat","clearask")],
             [b("💾 Backups MEGA","backups","primary")]])

    def paginate(self, items, page, make_row, prefix, extra=None, *, columns=1):
        pages = max(1,(len(items)+7)//8)
        page = min(max(0,page),pages-1)
        rows = [make_row(item) for item in items[page*8:(page+1)*8]]
        if columns > 1:
            buttons = [button for row in rows for button in row]
            rows = [buttons[i:i+columns] for i in range(0,len(buttons),columns)]
        navigation = []
        if page:
            navigation.append(b("⬅️ Anterior",f"{prefix}:{page-1}"))
        if page+1 < pages:
            navigation.append(b("Siguiente ➡️",f"{prefix}:{page+1}"))
        if navigation:
            rows.append(navigation)
        if extra:
            rows += extra
        return rows+BACK

    async def contact_list(self, page=0, action="contacts"):
        items = self.store.contacts(favorites=action=="favorites")
        send = action in {"sendlist","calllist"}
        title = "Elegí quién recibirá el SMS" if send else "Favoritos" if action=="favorites" else "Números guardados"
        rows = self.paginate(items,page,lambda c:[b("👤 "+("⭐ " if c["favorite"] else "")+c["name"],f"voicechoose:c:{c['id']}" if action=="calllist" else f"compose:c:{c['id']}" if send else f"contact:{c['id']}")],action,[[b("➕ Agregar número","contactadd","success")]],columns=2)
        await self.screen.show(f"<b>📒 {title}</b>\n"+(f"{len(items)} contactos." if items else "Todavía no hay contactos en esta lista."),rows,action)

    async def contact_view(self, contact_id):
        contact = self.store.contact(contact_id)
        if not contact:
            return await self.contact_list()
        profile = self.profiles.get(contact["preferred_api"]) if contact["preferred_api"] else None
        text = f"<b>📒 {h(contact['name'])}</b>\n{h(contact['phone'])}\nAPI preferida: {h(profile['label']) if profile else 'Rotación automática'}\nPermiso de SMS: {'✅ Confirmado' if contact['consent'] else '🔴 Pendiente'}\nPermiso de llamadas: {'✅ Confirmado' if contact['voice_consent'] else '🔴 Pendiente'}"
        if contact.get("simulation_only"):
            text += "\n\n🧪 Contacto ficticio: solo permite simulaciones locales, sin Twilio ni costo."
        rows = [[b("✉️ Enviar SMS",f"compose:c:{contact_id}","success")],
                [b("📞 Llamar",f"voicechoose:c:{contact_id}"),b("Permiso llamadas",f"voiceconsentask:{contact_id}")],
                [b("Quitar favorito" if contact["favorite"] else "⭐ Hacer favorito",f"star:{contact_id}"),b("✏️ Renombrar",f"crename:{contact_id}")],
                [b("🔌 Asignar API",f"assign:{contact_id}"),b("Permiso SMS",f"consentask:{contact_id}")],
                [b("🗑 Eliminar",f"cdelask:{contact_id}","danger")],[b("⬅️ Números","contacts:0")]]+BACK
        await self.screen.show(text,rows,f"contact:{contact_id}")

    async def group_list(self,page=0,send=False,calls=False):
        items = self.store.groups()
        prefix = "callgroups" if calls else "sendgroups" if send else "groups"
        rows = self.paginate(items,page,lambda g:[b(f"{g['name']} · {g['count']}",f"voicechoose:g:{g['id']}" if calls else f"compose:g:{g['id']}" if send else f"group:{g['id']}")],prefix,[[b("➕ Crear grupo","groupadd","success")]])
        await self.screen.show("<b>👥 "+("Elegí un grupo" if send else "Grupos")+"</b>\n"+("Seleccioná una opción." if items else "Todavía no hay grupos."),rows,prefix)

    async def group_view(self,group_id):
        group = self.store.group(group_id)
        if not group:
            return await self.group_list()
        members = self.store.members(group_id)
        text = f"<b>👥 {h(group['name'])}</b>\n{len(members)} integrantes.\n"+"\n".join("• "+h(c["name"]) for c in members[:15])
        if members and all(c.get("simulation_only") for c in members):
            text += "\n\n🧪 Grupo de simulación: no envía SMS reales."
        if len(members)>15:
            text += f"\nY {len(members)-15} más."
        await self.screen.show(text,[[b("✉️ Enviar al grupo",f"compose:g:{group_id}","success")],
            [b("➕ / ➖ Integrantes",f"members:{group_id}:0"),b("✏️ Renombrar",f"grename:{group_id}")],
            [b("🗑 Eliminar grupo",f"gdelask:{group_id}","danger")],[b("⬅️ Grupos","groups:0")]]+BACK,f"group:{group_id}")

    async def members_view(self,group_id,page):
        group = self.store.group(group_id)
        if not group:
            return await self.group_list()
        members = {c["id"] for c in self.store.members(group_id)}
        rows = self.paginate(self.store.contacts(),page,lambda c:[b(("✅ " if c["id"] in members else "➕ ")+c["name"],f"member:{group_id}:{c['id']}:{page}")],f"members:{group_id}",[[b("Listo",f"group:{group_id}","success")]])
        await self.screen.show(f"<b>Integrantes de {h(group['name'])}</b>\nTocá un contacto para agregarlo o quitarlo. Se guarda cada cambio.",rows,f"members:{group_id}")

    async def apis(self):
        profiles = self.profiles.all()
        text = "<b>🔌 APIs Twilio</b>\nAgregá una cuenta o API Key. La clave queda en .env y se borra del mensaje recibido."
        rows = [[b(("🟢 " if p["enabled"] else "🔴 ")+p["label"]+(" · PRUEBA" if p["mode"]=="test" else ""),"api:"+p["id"])] for p in profiles]
        rows += [[b("➕ Añadir API","apiadd","success"),b("📊 Estado y saldo","apistatus","primary")],
                 [b("🔗 Asignar API a número","assignlist:0")]]+BACK
        if not profiles:
            text += "\n\nNo hay APIs configuradas. Todavía no se pueden enviar SMS."
        await self.screen.show(text,rows,"apis")

    async def api_view(self,profile_id):
        p = self.profiles.get(profile_id)
        if not p:
            return await self.apis()
        pending = self.store.one("SELECT COUNT(*) n FROM recipients WHERE api_id=? AND status IN ('accepted','scheduled','queued','sending','sent')",(profile_id,))["n"]
        text = f"<b>🔌 {h(p['label'])}</b>\nEstado: {'habilitada' if p['enabled'] else 'deshabilitada'}\nModo: {'PRUEBA SIN SMS REALES' if p['mode']=='test' else 'Producción: puede consumir saldo'}\nRemitente SMS: {h(p['sender'])}\nCuenta: …{h(p['account_sid'][-6:])}\nEntregas pendientes de estado final: {pending}\nLas credenciales no se muestran."
        text += "\nNúmero para llamadas: "+h(voice.caller(p) or "Sin configurar")
        await self.screen.show(text,[[b("Desactivar" if p["enabled"] else "Activar","apitoggle:"+profile_id)],
            [b("✉️ Configurar remitente SMS","smssender:"+profile_id)],
            [b("📞 Configurar origen de llamadas","voicesender:"+profile_id)],
            [b("📊 Estado y saldo","apistatus"),b("🗑 Eliminar API","apidelask:"+profile_id,"danger")],[b("⬅️ APIs","apis")]]+BACK,"api:"+profile_id)

    async def status(self):
        await self.screen.show("<b>📊 Estado de APIs</b>\nConsultando Twilio…",[[b("⬅️ APIs","apis")]]+BACK,"apistatus")
        if not self.status_task or self.status_task.done():
            self.status_task = self.spawn(self.load_status())

    async def load_status(self):
        try:
            profiles = self.profiles.all()
            lines = ["<b>📊 Estado y saldo</b>"]
            for profile in profiles:
                result = await self.engine.provider.health(profile)
                lines.append(f"\n<b>{h(profile['label'])}</b> · {h(result['status'])}")
                if result.get("description"):
                    lines.append(h(result["description"]))
                if result.get("balance") is not None:
                    lines.append(f"Saldo: {h(str(result['balance']))} {h(str(result.get('currency','')))}")
                    prices = await self.engine.provider.prices(profile,"AR")
                    if prices:
                        lines.append(f"Argentina: {h(prices['min'])}–{h(prices['max'])} {h(prices['currency'])} / segmento")
                        try:
                            if str(result.get("currency","")).upper() == str(prices["currency"]).upper():
                                balance = max(Decimal("0"),Decimal(result["balance"]))
                                low, high = int(balance/Decimal(prices["max"])),int(balance/Decimal(prices["min"]))
                                lines.append(f"Aproximadamente {low}–{high} segmentos a Argentina; no es una cantidad garantizada.")
                        except (InvalidOperation,TypeError,ZeroDivisionError):
                            pass
            if not profiles:
                lines.append("No hay APIs configuradas.")
            lines.append("\nTwilio trabaja con saldo. El costo cambia por destino, remitente y longitud. Las subcuentas pueden compartir el saldo principal.")
            if self.store.state("view")=="apistatus":
                await self.screen.show("\n".join(lines),[[b("🔄 Actualizar","apistatus")],[b("⬅️ APIs","apis")]]+BACK,"apistatus",expected_view="apistatus")
        except Exception as exc:
            log.warning("api_status_failed category=%s",type(exc).__name__)

    async def assign(self,contact_id):
        contact = self.store.contact(contact_id)
        if not contact:
            return await self.contact_list()
        rows = [[b(("✅ " if contact["preferred_api"]==p["id"] else "")+p["label"],f"setapi:{contact_id}:{p['id']}")] for p in self.profiles.all()]
        rows += [[b("🔄 Rotación automática",f"setapi:{contact_id}:none")],[b("⬅️ Contacto",f"contact:{contact_id}")]]+BACK
        await self.screen.show(f"<b>API preferida · {h(contact['name'])}</b>\nSe intenta primero. Otra API solo se usa ante un rechazo que admita una alternativa segura.",rows,"assign")

    async def voice_permission(self,contact_id,resume=False):
        contact=self.store.contact(contact_id)
        if not contact:return await self.contact_list()
        suffix=":call" if resume else ""
        await self.screen.show(f"<b>📞 Permiso de llamadas · {h(contact['name'])}</b>\n¿Esta persona aceptó recibir tus llamadas? El permiso de SMS se guarda por separado. Confirmar este permiso no inicia ninguna llamada.",
            [[b("✅ Sí, tiene permiso",f"voiceconsent:{contact_id}:1{suffix}","success")],
             [b("Retirar permiso",f"voiceconsent:{contact_id}:0","danger")],
             [b("⬅️ Contacto",f"contact:{contact_id}")]]+BACK,"voiceconsent")

    async def choose_voice(self,kind,item_id):
        if self.engine.busy:raise ValueError("Esperá a que termine el envío actual.")
        item=self.store.contact(item_id) if kind=="c" else self.store.group(item_id)
        if not item:raise ValueError("El destinatario ya no existe.")
        if kind=="c" and not item["voice_consent"]:
            return await self.voice_permission(item_id,resume=True)
        await self.screen.show(f"<b>📞 Llamada a {h(item['name'])}</b>\nElegí qué hacer. Antes de llamar se pedirá confirmación.\n\nSolo llamar: suena hasta 20 segundos y corta al atender.\nLeer texto: una voz básica en español lee tu mensaje y corta. La llamada puede consumir saldo.",
            [[b("📞 Solo llamar",f"voicecompose:{kind}:{item_id}:voice_ring")],[b("🗣 Leer un texto",f"voicepick:{kind}:{item_id}")],[b("🎵 Reproducir audio MP3",f"voicecompose:{kind}:{item_id}:voice_audio")]]+
            ([[b('📚 Audios guardados',f'audios:{kind}:{item_id}')]] if self.media.assets else [])+BACK,"voicechoose")

    async def saved_audios(self,kind,item_id,alias=None):
        if kind not in {'c','g'}:raise ValueError('Elegí un destinatario válido.')
        if not self.media.assets:raise ValueError('Los audios guardados requieren Twilio Assets.')
        item=self.store.contact(item_id) if kind=='c' else self.store.group(item_id)
        if not item:raise ValueError('El destinatario ya no existe.')
        self.flow=None
        if alias:
            audio=self.media.saved(alias)
            text=f"<b>🎵 {h(audio['name'])}</b>\nDuración: {audio['seconds']:.1f} segundos.\n"+('Disponible en Twilio para reutilizar.' if audio['published'] else 'Se publicará en Twilio al confirmar la primera llamada.')
            text+='\n\nUsarlo prepara una confirmación; todavía no llama. La llamada mantiene su costo habitual.'
            rows=[[b('📞 Usar este audio',f'audiouse:{kind}:{item_id}:{alias}','success')],
                  [b('🗑 Eliminar guardado',f'audiodelask:{kind}:{item_id}:{alias}','danger')],
                  [b('⬅️ Audios',f'audios:{kind}:{item_id}')]]+BACK
        else:
            saved=self.media.library()
            text='<b>📚 Audios guardados</b>\n'+(f'{len(saved)} audios reutilizables. Elegí uno para preparar la llamada.' if saved else 'Todavía no guardaste audios. Al adjuntar un MP3 podés marcar «Guardar para reutilizar» antes de confirmar.')
            rows=[[b('🎵 '+audio['name'][:40],f'audiodetail:{kind}:{item_id}:{audio["token"][:12]}')] for audio in saved]
            rows+=[[b('⬅️ Volver',f'voicechoose:{kind}:{item_id}')]]+BACK
        await self.screen.show(text,rows,'audios')

    async def select_voice(self,kind,item_id):
        if self.engine.busy:raise ValueError("Esperá a que termine el envío actual.")
        item=self.store.contact(item_id) if kind=="c" else self.store.group(item_id)
        if not item:raise ValueError("El destinatario ya no existe.")
        if kind=="c" and not item["voice_consent"]:
            return await self.voice_permission(item_id,resume=True)
        preferred=self.store.state("preferred_tts_voice",voice.DEFAULT_VOICE)
        rows=[[b(("✅ " if ident==preferred else "")+value["label"],f"voicecompose:{kind}:{item_id}:voice_text:{ident}")] for ident,value in voice.VOICES.items()]
        rows+=[[b("⬅️ Volver",f"voicechoose:{kind}:{item_id}")]]+BACK
        await self.screen.show(f"<b>🗣 Voz para {h(item['name'])}</b>\nElegí la voz que leerá tu texto. Las voces básicas no tienen cargo adicional por síntesis; la llamada mantiene su costo habitual. Elegir una voz no inicia ninguna llamada.",rows,"voicepick")

    async def make_draft(self,kind,item_id,text,channel="sms",tts_voice=voice.DEFAULT_VOICE,*,audio_token='',audio_seconds=0):
        if self.backups.status().get('state')=='running':raise ValueError('Esperá a que termine la operación de backup.')
        item=self.store.contact(item_id) if kind=="c" else self.store.group(item_id)
        if not item:raise ValueError("El destinatario ya no existe.")
        contacts=[item] if kind=="c" else self.store.members(item_id)
        if len(contacts)>self.settings.max_recipients:raise ValueError("El grupo supera el límite de destinatarios configurado.")
        modes={"simulation"} if contacts and all(c.get("simulation_only") for c in contacts) else {p["mode"] for p in self.engine.available("live",channel)}
        if len(modes)!=1:raise ValueError("Activá APIs del mismo modo y compatibles con este tipo de envío.")
        old=self.store.state("active_draft")
        if old and self.store.campaign(old) and self.store.campaign(old)["status"]=="draft":self.store.cancel(old)
        cid=self.store.draft(item["name"],text,contacts,next(iter(modes)),channel,tts_voice=tts_voice,audio_token=audio_token,audio_seconds=audio_seconds)
        if channel=='voice_audio':
            try:self.media.attach(audio_token,cid)
            except ValueError:self.store.cancel(cid);raise
        self.store.set_state("active_draft",cid);self.flow=None
        return await self.confirmation(cid)

    async def compose(self,kind,item_id,channel="sms",tts_voice=None):
        if self.engine.busy:
            return await self.main("Ya hay un envío en ejecución.")
        item = self.store.contact(item_id) if kind=="c" else self.store.group(item_id)
        if not item:
            return await self.main("El destinatario ya no existe.")
        if channel!="voice_text":tts_voice=voice.DEFAULT_VOICE
        elif tts_voice is None:tts_voice=self.store.state("preferred_tts_voice",voice.DEFAULT_VOICE)
        selected=voice.preset(tts_voice)
        contacts = [item] if kind=="c" else self.store.members(item_id)
        if channel!="sms" and kind=="c" and not item["voice_consent"]:
            return await self.voice_permission(item_id,resume=True)
        simulated = bool(contacts) and all(c.get("simulation_only") for c in contacts)
        if any(c.get("simulation_only") for c in contacts) and not simulated:
            raise ValueError("Separá los contactos simulados de los reales en grupos distintos.")
        if not simulated and not self.engine.available("live",channel):
            if channel!="sms":
                rows=[[b("🔌 Configurar API y origen","apis")]]+BACK
                return await self.screen.show("<b>📞 Falta el número de origen</b>\nConfigurá en la API un número Twilio o un identificador de llamada verificado. El nombre alfanumérico de SMS no sirve para llamadas. No se inició ninguna llamada.",rows,"voiceorigin")
            return await self.apis()
        if channel=="voice_ring":
            return await self.make_draft(kind,item_id,"Solo llamada: cortar al atender.",channel)
        if channel=='voice_audio':
            if not self.media.enabled:raise ValueError('Las llamadas con MP3 todavía no están configuradas en este servidor.')
            self.flow=('audio_upload',{'kind':kind,'id':item_id})
            return await self.screen.show(f"<b>🎵 Audio para {h(item['name'])}</b>\nAdjuntá tu archivo MP3 como audio o documento. Máximo: 20 MB y 40 minutos. Se reproducirá una vez al atender. Después revisarás y confirmarás los destinatarios. Todavía no se inicia ninguna llamada.",[[b('Cancelar','cancel','danger')]]+BACK,'audio_upload')
        self.flow = ("compose",{"kind":kind,"id":item_id,"channel":channel,"tts_voice":tts_voice})
        if channel=="voice_text":self.store.set_state("preferred_tts_voice",tts_voice)
        note = "\n🧪 SIMULACIÓN LOCAL · Sin SMS reales ni consumo de saldo.\n" if simulated else "\n"
        instruction=f"Voz: {selected['label']}.\nEscribí el texto que escuchará al atender, hasta 600 caracteres." if channel=="voice_text" else "Escribí el texto."
        await self.screen.show(f"<b>✉️ Mensaje para {h(item['name'])}</b>{note}{instruction} Después vas a poder revisarlo y confirmar o cancelar. Todavía no se envía nada.",[[b("Cancelar","cancel","danger")]]+BACK,"compose")

    async def audio_attachment(self,attachment):
        if not self.flow or self.flow[0]!='audio_upload':raise ValueError('Elegí primero Enviar llamada → Reproducir audio MP3.')
        values=self.flow[1]
        try:token,meta=await self.media.receive(self.telegram,attachment)
        except TelegramError:raise ValueError('Telegram no pudo entregar el MP3. Verificá que no supere 20 MB y adjuntalo nuevamente.') from None
        try:
            return await self.make_draft(values['kind'],values['id'],meta['name'],'voice_audio',audio_token=token,audio_seconds=meta['seconds'])
        except BaseException:
            if not meta.get('reusable'):self.media.remove(token)
            raise

    async def confirmation(self,campaign_id):
        campaign = self.store.campaign(campaign_id)
        if not campaign or campaign["status"]!="draft":
            return await self.campaign_view(campaign_id)
        recipients = self.store.recipients(campaign_id)
        size = message_size(campaign["body"])
        mode = ("🧪 SIMULACIÓN LOCAL: identificadores internos, sin conexión a Twilio y sin costo." if campaign["mode"]=="simulation"
                else "🧪 PRUEBA OFICIAL: no se entrega un SMS real." if campaign["mode"]=="test"
                else "💳 ENVÍO REAL: puede consumir saldo de Twilio.")
        text = f"<b>Confirmar envío · {h(campaign['title'])}</b>\n{mode}\n\n{len(recipients)} destinatarios · {size.encoding}\n{size.segments} segmentos por persona · {size.segments*len(recipients)} segmentos en total.\n\n<b>Texto completo:</b>\n{h(campaign['body'])}\n\n"
        is_call=campaign.get("channel","sms")!="sms"
        if is_call:
            kind={'voice_text':'Leer texto en español','voice_audio':'Reproducir audio MP3','voice_ring':'Solo llamar'}[campaign['channel']]
            text=f"<b>📞 Confirmar llamadas · {h(campaign['title'])}</b>\n{mode}\n\n{len(recipients)} destinatarios.\nTipo: {kind}\n\n{h(campaign['body'])}\n\nLa llamada se cobra según destino y duración; puede aplicar redondeo. El origen será un número habilitado.\n\n"
            if campaign['channel']=='voice_audio':
                saved=bool(self.media.metadata(campaign['audio_token']).get('reusable'))
                retention='Guardado para reutilizar: se conserva hasta que lo elimines.' if saved else 'Temporal: se elimina del VPS y Twilio al terminar todas las llamadas.'
                text+=f"Duración del audio: {campaign['audio_seconds']:.1f} segundos. Se reproduce una vez.\n{retention}\n\n"
            if campaign["channel"]=="voice_text":text+="Voz: "+h(voice.preset(campaign.get("tts_voice",voice.DEFAULT_VOICE))["label"])+"\n\n"
            if campaign["mode"]!="simulation":
                origins=list(dict.fromkeys(voice.caller(p) for p in self.engine.available(campaign["mode"],campaign["channel"]) if p["mode"]==campaign["mode"]))
                text+="<b>Número(s) de origen configurado(s):</b> "+", ".join(h(origin) for origin in origins)+"\nEs el identificador de llamada solicitado a Twilio. Debe estar habilitado en esa cuenta.\n\n"
        text += "\n".join(h(r["contact_name"])+" · "+h(r["phone"]) for r in recipients[:8])
        if len(recipients)>8:
            text += f"\nY {len(recipients)-8} más; revisalos en Destinatarios."
        text += ("\n\nVista de prueba del envío grupal. Ningún teléfono recibirá este mensaje." if campaign["mode"]=="simulation"
                 else "\n\nConfirmá solo si los destinatarios aceptaron recibir estas llamadas." if is_call
                 else "\n\nAl confirmar indicás que estos destinatarios aceptaron recibir este mensaje. Aceptado por Twilio no significa entregado al celular.")
        rows=[[b("🧪 CONFIRMAR SIMULACIÓN" if campaign["mode"]=="simulation" else "📞 CONFIRMAR LLAMADAS" if is_call else "✅ CONFIRMAR ENVÍO","confirm:"+campaign_id,"success")]]
        if campaign['channel']=='voice_audio' and self.media.assets:
            rows.append([b('✅ Guardado para reutilizar' if saved else '💾 Guardar para reutilizar','audiokeep:'+campaign_id)])
        rows.append([b("❌ Cancelar envío","cancelcampaign:"+campaign_id,"danger"),b("👥 Destinatarios",f"recipients:{campaign_id}:0")])
        await self.screen.show(text,rows,"draft:"+campaign_id)

    async def progress(self,campaign_id):
        if self.store.state("view") == "campaign:"+campaign_id:
            await self.campaign_view(campaign_id,only_if_view=True)

    async def campaign_view(self,campaign_id,only_if_view=False):
        campaign = self.store.campaign(campaign_id)
        if not campaign:
            return await self.history()
        summary = self.engine.summary(campaign_id)
        state = {"running":"En curso", "queued":"En cola", "completed":"Finalizado", "stopped":"Detenido", "interrupted":"Interrumpido", "cancelled":"Cancelado", "draft":"Sin confirmar"}.get(campaign["status"], campaign["status"])
        simulated = campaign["mode"]=="simulation"
        banner = "\n🧪 SIMULACIÓN · Sin SMS reales ni costo.\n" if simulated else "\n"
        text = f"<b>📨 {h(campaign['title'])}</b>{banner}Estado: {h(state)}\n\n👥 Total: {summary['total']}\n✅ Entregados al celular: {summary['delivered']}\n📤 Aceptados / en tránsito: {summary['accepted']}\n❌ Fallidos: {summary['failed']}\n❔ Resultado incierto: {summary['unknown']}\n⏸ Cancelados: {summary['cancelled']}\n⏳ Sin procesar: {summary['pending']}"
        is_call=campaign.get("channel","sms")!="sms"
        if is_call:
            text=f"<b>📞 {h(campaign['title'])}</b>{banner}Estado del lote: {h(state)}\n\n👥 Total: {summary['total']}\n✅ Llamadas completadas: {summary['voice_completed']}\n📞 En cola / sonando / en curso: {summary['voice_active']}\n📵 Sin respuesta: {summary['no_answer']}\n☎ Ocupado: {summary['busy']}\n❌ Fallidas: {summary['failed']}\n❔ Inciertas: {summary['unknown']}\n⏸ Canceladas: {summary['cancelled']}\n⏳ Sin procesar: {summary['pending']}"
        if summary["simulated"]:
            text += f"\n🧪 Pruebas sin envío real: {summary['simulated']}"
        errors = [r for r in self.store.recipients(campaign_id) if r["error"] and r["status"] in {"failed","undelivered","unknown"}]
        if errors:
            text += "\n\n"+"\n".join(h(r["contact_name"])+": "+h(r["error"]) for r in errors[-4:])
        if simulated:
            processed = summary["total"]-summary["pending"]
            filled = round(processed*10/summary["total"]) if summary["total"] else 0
            text += "\n\n" + "🟩"*filled + "⬜"*(10-filled) + f" {processed}/{summary['total']}"
            text += "\n\n<b>Destinatarios ficticios:</b>\n" + "\n".join(("🧪 " if r["status"]=="simulated" else "⏳ " if r["status"] in {"pending","submitting"} else "⏸ ")+h(r["contact_name"])+" · "+h(r["phone"]) for r in self.store.recipients(campaign_id)[:8])
            text += "\n\nDemostración del flujo grupal. Twilio no participa y no se descuenta saldo."
        elif is_call:
            text += "\n\nDetener frena las próximas solicitudes; no corta llamadas ya iniciadas. Completada indica conexión finalizada, no confirma quién atendió ni que haya escuchado el texto."
        else:
            text += "\n\nLos estados de entrega se actualizan mientras Twilio los informa. Detener frena los siguientes envíos; no retira un SMS ya aceptado."
        rows = [[b("🔎 Ver destinatarios",f"recipients:{campaign_id}:0"),b("🔄 Actualizar","campaign:"+campaign_id)]]
        if campaign["status"] in {"queued","running"}:
            rows.insert(0,[b("🛑 DETENER","stop:"+campaign_id,"danger")])
        rows += [[b("📋 Historial","history:0")]]+BACK
        await self.screen.show(text,rows,"campaign:"+campaign_id,expected_view="campaign:"+campaign_id if only_if_view else None)

    async def recipient_view(self,campaign_id,page):
        campaign = self.store.campaign(campaign_id)
        if not campaign:
            return await self.history()
        items = self.store.recipients(campaign_id)
        page = min(max(0,page),max(0,(len(items)-1)//8))
        lines = [f"<b>Destinatarios · {h(campaign['title'])}</b>"]
        for r in items[page*8:(page+1)*8]:
            lines.append(f"\n<b>{h(r['contact_name'])}</b> · {h(r['phone'])}\n{h(LABELS.get(r['status'],r['status']))}")
            if r["error"]:
                lines.append(h(r["error"]))
        rows = []
        nav = []
        if page:
            nav.append(b("⬅️ Anterior",f"recipients:{campaign_id}:{page-1}"))
        if (page+1)*8<len(items):
            nav.append(b("Siguiente ➡️",f"recipients:{campaign_id}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([b("⬅️ Confirmación" if campaign["status"]=="draft" else "⬅️ Envío",("draft:" if campaign["status"]=="draft" else "campaign:")+campaign_id)])
        await self.screen.show("\n".join(lines),rows+BACK,"recipients:"+campaign_id)

    async def history(self,page=0):
        campaigns = self.store.rows("SELECT * FROM campaigns WHERE status!='draft' ORDER BY created DESC LIMIT 200")
        rows = self.paginate(campaigns,page,lambda c:[b(c["created"][:16].replace("T"," ")+" · "+c["title"][:25]+" · "+c["status"],"campaign:"+c["id"])],"history")
        await self.screen.show("<b>📋 Historial de envíos</b>\n"+("Fechas en UTC. Cada envío conserva sus destinatarios y estados." if campaigns else "Todavía no hay envíos."),rows,"history")

    async def prompt(self,kind,text,data=None):
        self.flow = (kind,data or {})
        await self.screen.show(text,[[b("Cancelar","cancel","danger")]]+BACK,kind)

    async def cancel(self):
        active = self.store.state("active_draft")
        if active:
            campaign = self.store.campaign(active)
            if campaign and campaign["status"]=="draft":
                self.store.cancel(active)
        self.media.cleanup(self.store)
        self.store.set_state("active_draft",None)
        self.flow = None
        await self.main("Operación cancelada. No se enviaron mensajes pendientes de confirmación.")

    async def help(self):
        await self.screen.show("<b>ℹ️ Cómo usar el bot</b>\n1. Agregá una API de Twilio y un remitente habilitado.\n2. Guardá contactos; verificá país y característica. La validación del formato no confirma que la línea exista.\n3. Escribí un mensaje, revisá destinatarios y confirmá.\n\nPodés escribir «enviar Ana» o «mandar grupo Colegio»; el bot propone coincidencias y nunca envía sin confirmación.\n\nLas APIs se rotan y se respeta la preferida. Ante una conexión rechazada o límite temporal se reintenta una vez después de 5 segundos. No se cambian APIs para superar bajas, filtros o restricciones de Twilio. Si se pierde la respuesta del envío, queda incierto y no se repite para evitar cobros duplicados.\n\nLos contactos, grupos y estados se guardan en el servidor. Las credenciales van en .env y las acciones en bot.log. Un reinicio no vuelve a enviar una campaña interrumpida.\n\n/limpiar elimina mensajes que el bot conoce y Telegram permite borrar; no puede borrar toda la historia ajena o antigua del chat.",BACK,"help")

    async def backups_menu(self,only_if_view=False):
        status=self.backups.status()
        pending=self.store.state('backup_pending')
        if pending and status.get('id')!=pending:status={'state':'running','percent':0,'message':'Operación en cola; preparando el backup.'}
        text='<b>💾 Backups MEGA</b>\nExporta contactos, grupos, favoritos, APIs e historial en una bóveda VeraCrypt AES-256.\n\n'
        if status:
            text+=h(status.get('message',''))
            if status.get('state')=='running':
                percent=min(100,max(0,int(status.get('percent',0))))
                text+='\n'+'🟩'*(percent//10)+'⬜'*(10-percent//10)+f' {percent}%'
        else:text+='Todavía no se creó una copia desde este bot.'
        text+='\n\nImportar descarga y descifra en el VPS, valida el contenido, reemplaza los datos y reinicia solo este bot. Se conserva la autorización de Telegram. Los temporales se eliminan.'
        rows=[[b('📤 Exportar a MEGA','backupexportask','success')],[b('📥 Importar enlace MEGA','backupimport','primary')],[b('🔄 Actualizar','backups')]]
        link=status.get('url') or self.store.state('last_backup_url')
        if link:
            try:rows.insert(0,[{'text':'⬇ Descargar último backup','url':download_url(link)}])
            except ValueError:pass
        await self.screen.show(text,rows+BACK,'backups',expected_view='backups' if only_if_view else None)

    async def backup_watch(self):
        status=self.backups.status()
        ident=status.get('id')
        key=(ident,status.get('state'),status.get('percent'))
        if key==self.backup_notice:return
        self.backup_notice=key
        if ident and ident==self.store.state('backup_pending') and status.get('state') in {'completed','error'}:self.store.set_state('backup_pending',None)
        if status.get('state')=='completed' and status.get('url'):
            self.store.set_state('last_backup_url',download_url(status['url']))
        if self.store.state('view')=='backups':await self.backups_menu(only_if_view=True)
        if ident and status.get('state') in {'completed','error'} and self.store.state('backup_notified')!=ident:
            self.store.set_state('backup_notified',ident)
            if self.store.state('view')!='backups':
                result=await self.telegram.call('sendMessage',chat_id=self.settings.chat_id,text=('✅ ' if status['state']=='completed' else '❌ ')+h(status.get('message','Operación terminada.')),parse_mode='HTML')
                self.store.track(self.settings.chat_id,result['message_id'])

    async def callback(self,data):
        parts = data.split(":")
        op = parts[0]
        if op in {"main","contacts","favorites","sendlist","calllist","groups","sendgroups","callgroups","apis","history","apistatus","help","backups"}:
            self.flow = None
        if op=="main": return await self.main()
        if op=='backups':return await self.backups_menu()
        if op=='backupexportask':
            self.flow=('backup_export_confirm',{})
            return await self.screen.show('¿Crear una copia cifrada y subirla a MEGA? Incluye números y claves API. La copia anterior se elimina solo después de verificar la nueva.',[[b('✅ Exportar','backupexport','success')],[b('Cancelar','backups')]],'backupconfirm')
        if op=='backupimport':return await self.prompt('backup_link','Pegá el enlace completo del backup MEGA. Se borrará el mensaje recibido para no dejar el enlace con su clave en el chat.')
        if op in {'backupexport','backuprestore'}:
            if self.engine.busy:raise ValueError('Detené o esperá a que termine el envío antes de usar backups.')
            if op=='backupexport' and (not self.flow or self.flow[0]!='backup_export_confirm'):raise ValueError('Volvé a confirmar la exportación del backup.')
            if op=='backuprestore' and (not self.flow or self.flow[0]!='backup_confirm'):raise ValueError('La confirmación de importación ya no está vigente.')
            url=self.flow[1]['url'] if op=='backuprestore' else None
            request_id=self.backups.request('import' if op=='backuprestore' else 'export',url)
            self.store.set_state('backup_pending',request_id)
            self.flow=None
            return await self.backups_menu()
        if op=="help": return await self.help()
        if op in {"contacts","favorites","sendlist","calllist"}: return await self.contact_list(int(parts[1]),op)
        if op=="callgroups":return await self.group_list(int(parts[1]),True,True)
        if op=="voicechoose":return await self.choose_voice(parts[1],int(parts[2]))
        if op=="voicepick":return await self.select_voice(parts[1],int(parts[2]))
        if op=="voicecompose":return await self.compose(parts[1],int(parts[2]),parts[3],parts[4] if len(parts)==5 else None)
        if op=='audios':return await self.saved_audios(parts[1],int(parts[2]))
        if op=='audiodetail':return await self.saved_audios(parts[1],int(parts[2]),parts[3])
        if op=='audiouse':
            if self.engine.busy:raise ValueError('Esperá a que termine el envío actual.')
            audio=self.media.saved(parts[3])
            return await self.make_draft(parts[1],int(parts[2]),audio['name'],'voice_audio',audio_token=audio['token'],audio_seconds=audio['seconds'])
        if op=='audiokeep':
            campaign=self.store.campaign(parts[1])
            if self.store.state('active_draft')!=parts[1] or not campaign or campaign['status']!='draft' or campaign['channel']!='voice_audio':raise ValueError('Volvé a preparar el audio antes de cambiar su conservación.')
            value=self.media.metadata(campaign['audio_token'])
            self.media.set_reusable(campaign['audio_token'],not value.get('reusable'))
            return await self.confirmation(parts[1])
        if op=='audiodelask':
            audio=self.media.saved(parts[3])
            return await self.screen.show(f"¿Eliminar <b>{h(audio['name'])}</b> del VPS y Twilio? Si hay llamadas usando el audio, se conservará hasta que terminen.",
                [[b('🗑 Eliminar',f'audiodel:{parts[1]}:{parts[2]}:{parts[3]}','danger')],[b('Cancelar',f'audiodetail:{parts[1]}:{parts[2]}:{parts[3]}')]]+BACK,'audio_delete')
        if op=='audiodel':
            audio=self.media.saved(parts[3]);self.media.forget(audio['token'])
            await self.engine.cleanup_audio()
            return await self.saved_audios(parts[1],int(parts[2]))
        if op=="voiceconsentask":
            return await self.voice_permission(int(parts[1]))
        if op=="voiceconsent":
            self.store.execute("UPDATE contacts SET voice_consent=? WHERE id=?",(int(parts[2])==1,int(parts[1])))
            if parts[2]=="1" and parts[3:]==["call"]:
                return await self.choose_voice("c",int(parts[1]))
            return await self.contact_view(int(parts[1]))
        if op=="voicesender":return await self.prompt("voice_sender","Ingresá el número Twilio o verificado que se mostrará al llamar, con +país. Escribí quitar para retirar el origen específico.",{"id":parts[1]})
        if op=="smssender":
            p=self.profiles.get(parts[1])
            if not p:return await self.apis()
            return await self.prompt("sms_sender",f"<b>✉️ Remitente SMS · {h(p['label'])}</b>\nActual: {h(p['sender'])}\n\nIngresá tu número Twilio habilitado para SMS con +país, un nombre alfanumérico de hasta 11 caracteres o un Messaging Service SID MG. En Argentina puede mostrarse un código corto en lugar del remitente configurado. El origen de llamadas se conserva.",{"id":p["id"]})
        if op=="contact": return await self.contact_view(int(parts[1]))
        if op=="contactadd": return await self.prompt("contact_name","<b>➕ Nuevo número</b>\nEscribí el nombre del contacto.")
        if op=="star":
            self.store.execute("UPDATE contacts SET favorite=1-favorite WHERE id=?",(int(parts[1]),))
            log.info("contact_favorite id=%s",parts[1])
            return await self.contact_view(int(parts[1]))
        if op=="crename": return await self.prompt("contact_rename","Escribí el nuevo nombre.",{"id":int(parts[1])})
        if op=="cdelask":
            c = self.store.contact(int(parts[1]))
            if not c: return await self.contact_list()
            return await self.screen.show(f"¿Eliminar <b>{h(c['name'])}</b> y quitarlo de sus grupos? El historial previo se conserva.",[[b("Eliminar",f"cdel:{c['id']}","danger"),b("Cancelar",f"contact:{c['id']}")]],"delete_contact")
        if op=="cdel":
            self.store.delete_contact(int(parts[1])); log.info("contact_deleted id=%s",parts[1])
            return await self.contact_list()
        if op=="consentask":
            cid = int(parts[1]); c = self.store.contact(cid)
            if not c: return await self.contact_list()
            return await self.screen.show(f"<b>Permiso · {h(c['name'])}</b>\nConfirmá solo si esta persona aceptó recibir tus SMS. Podés retirar el permiso para impedir próximos envíos.",[[b("✅ Tiene permiso",f"consent:{cid}:1","success")],[b("Retirar permiso",f"consent:{cid}:0","danger")],[b("⬅️ Volver",f"contact:{cid}")]],"consent")
        if op=="consent":
            self.store.execute("UPDATE contacts SET consent=? WHERE id=?",(int(parts[2])==1,int(parts[1])))
            log.info("contact_permission id=%s allowed=%s",parts[1],parts[2])
            return await self.contact_view(int(parts[1]))
        if op in {"groups","sendgroups"}: return await self.group_list(int(parts[1]),op=="sendgroups")
        if op=="group": return await self.group_view(int(parts[1]))
        if op=="groupadd": return await self.prompt("group_name","<b>➕ Crear grupo</b>\nEscribí el nombre del grupo.")
        if op=="members": return await self.members_view(int(parts[1]),int(parts[2]))
        if op=="member":
            self.store.toggle_member(int(parts[1]),int(parts[2])); log.info("group_membership_changed group=%s contact=%s",parts[1],parts[2])
            return await self.members_view(int(parts[1]),int(parts[3]))
        if op=="grename": return await self.prompt("group_rename","Escribí el nuevo nombre del grupo.",{"id":int(parts[1])})
        if op=="gdelask":
            g = self.store.group(int(parts[1]))
            if not g: return await self.group_list()
            return await self.screen.show(f"¿Eliminar el grupo <b>{h(g['name'])}</b>? Los contactos se conservan.",[[b("Eliminar",f"gdel:{g['id']}","danger"),b("Cancelar",f"group:{g['id']}")]],"delete_group")
        if op=="gdel":
            self.store.execute("DELETE FROM groups_ WHERE id=?",(int(parts[1]),)); log.info("group_deleted id=%s",parts[1])
            return await self.group_list()
        if op=="apis": return await self.apis()
        if op=="api": return await self.api_view(parts[1])
        if op=="apistatus": return await self.status()
        if op=="apiadd": return await self.prompt("api_label","<b>➕ Añadir API Twilio</b>\nEscribí un nombre para identificar esta conexión.")
        if op=="apiauth":
            if not self.flow or self.flow[0]!="api_username": return await self.apis()
            values = self.flow[1]; values["username"] = values["account_sid"]
            return await self.prompt("api_secret","Pegá el Auth Token. Voy a borrar el mensaje del chat al recibirlo.",values)
        if op=="apimode":
            if not self.flow or self.flow[0]!="api_mode": return await self.apis()
            values = self.flow[1]; values["mode"] = parts[1]
            profile = self.profiles.add(values)
            self.flow = None
            log.info("api_added id=%s mode=%s",profile["id"],profile["mode"])
            return await self.api_view(profile["id"])
        if op=="apitoggle":
            self.profiles.toggle(parts[1]); log.info("api_toggled id=%s",parts[1])
            return await self.api_view(parts[1])
        if op=="apidelask":
            p = self.profiles.get(parts[1])
            if not p: return await self.apis()
            return await self.screen.show(f"¿Eliminar <b>{h(p['label'])}</b> y sus credenciales? Los números asignados pasarán a rotación.",[[b("Eliminar","apidel:"+p["id"],"danger"),b("Cancelar","api:"+p["id"])]],"delete_api")
        if op=="apidel":
            if self.engine.busy or self.store.one("SELECT 1 FROM recipients WHERE api_id=? AND status IN ('accepted','scheduled','queued','sending','sent','ringing','in-progress')",(parts[1],)):
                raise ValueError("Hay envíos o entregas pendientes con esa API. Podés desactivarla; eliminála después de recibir los estados.")
            self.profiles.delete(parts[1]); self.store.execute("UPDATE contacts SET preferred_api=NULL WHERE preferred_api=?",(parts[1],)); log.info("api_deleted id=%s",parts[1])
            return await self.apis()
        if op=="assignlist":
            items = self.store.contacts()
            rows = self.paginate(items,int(parts[1]),lambda c:[b(c["name"],f"assign:{c['id']}")],"assignlist")
            return await self.screen.show("Elegí un contacto para asignarle su API preferida.",rows,"assignlist")
        if op=="assign": return await self.assign(int(parts[1]))
        if op=="setapi":
            profile = None if parts[2]=="none" else self.profiles.get(parts[2])
            if parts[2]!="none" and not profile: raise ValueError("Esa API ya no existe.")
            self.store.execute("UPDATE contacts SET preferred_api=? WHERE id=?",(profile["id"] if profile else None,int(parts[1])))
            log.info("contact_api_assigned contact=%s api=%s",parts[1],parts[2])
            return await self.contact_view(int(parts[1]))
        if op=="compose": return await self.compose(parts[1],int(parts[2]))
        if op=="draft": return await self.confirmation(parts[1])
        if op=="confirm":
            if self.backups.status().get('state')=='running':raise ValueError('Esperá a que termine el backup.')
            campaign = self.store.campaign(parts[1])
            if self.store.state("active_draft")!=parts[1] or not campaign:
                raise ValueError("La confirmación ya no está vigente. Prepará el mensaje otra vez.")
            if (datetime.now(timezone.utc)-datetime.fromisoformat(campaign["created"])).total_seconds()>1800:
                self.store.cancel(parts[1]); raise ValueError("La confirmación venció después de 30 minutos. Prepará el mensaje otra vez.")
            if await self.engine.confirm(parts[1]):
                self.store.set_state("active_draft",None)
            return await self.campaign_view(parts[1])
        if op=="cancelcampaign":
            self.store.cancel(parts[1]); self.store.set_state("active_draft",None)
            return await self.main("Envío cancelado antes de confirmar.")
        if op=="campaign": return await self.campaign_view(parts[1])
        if op=="recipients": return await self.recipient_view(parts[1],int(parts[2]))
        if op=="stop":
            self.engine.stop(parts[1]); return await self.campaign_view(parts[1])
        if op=="history": return await self.history(int(parts[1]))
        if op in {"cancel","cancelar"}: return await self.cancel()
        if op=="clearask":
            return await self.screen.show("¿Limpiar los mensajes conocidos de este chat? Los contactos y el historial de envíos se conservan. Telegram puede impedir borrar mensajes antiguos.",[[b("🧹 Limpiar","clear","danger"),b("Cancelar","main")]],"clear")
        if op=="clear":
            self.flow = None
            deleted,remaining = await self.screen.clear()
            return await self.main(f"Se eliminaron {deleted} mensajes. "+(f"Telegram no permitió borrar {remaining}." if remaining else ""))
        raise ValueError("La opción ya no está disponible.")

    async def text(self,text):
        if self.flow:
            kind,values = self.flow
            if kind=='backup_link':
                url=download_url(text)
                self.flow=('backup_confirm',{'url':url})
                return await self.screen.show('<b>Importar backup</b>\nSe reemplazarán contactos, grupos, APIs e historial con los de esta copia y se reiniciará el servicio. Se valida antes de aplicar y se eliminan los temporales. No se reanudan envíos.\n\n¿Confirmás la restauración?',[[b('⚠️ RESTAURAR BACKUP','backuprestore','danger')],[b('Cancelar','backups')]],'backupconfirm')
            if kind=="voice_sender":
                profiles=self.profiles.all()
                for p in profiles:
                    if p["id"]==values["id"]:p["voice_sender"]="" if text.strip().lower()=="quitar" else text.strip()
                self.profiles.save(profiles);self.flow=None
                return await self.api_view(values["id"])
            if kind=="sms_sender":
                profiles=self.profiles.all()
                selected=next((p for p in profiles if p["id"]==values["id"]),None)
                if not selected:
                    self.flow=None
                    return await self.apis()
                selected["sender"]=text.strip()
                self.profiles.save(profiles);self.flow=None
                return await self.api_view(values["id"])
            if kind=="contact_name":
                values["name"] = name(text)
                return await self.prompt("contact_phone","Ingresá el celular con código de país, solo dígitos y + opcional. Para Argentina suele empezar con 549. La validación no comprueba si la línea está activa.",values)
            if kind=="contact_phone":
                contact_id = self.store.add_contact(values["name"],text)
                self.flow = None; log.info("contact_added id=%s",contact_id)
                return await self.callback(f"consentask:{contact_id}")
            if kind=="contact_rename":
                self.store.rename_contact(values["id"],text); self.flow = None; log.info("contact_renamed id=%s",values["id"])
                return await self.contact_view(values["id"])
            if kind=="group_name":
                group_id = self.store.add_group(text); self.flow = None; log.info("group_created id=%s",group_id)
                return await self.members_view(group_id,0)
            if kind=="group_rename":
                self.store.rename_group(values["id"],text); self.flow = None; log.info("group_renamed id=%s",values["id"])
                return await self.group_view(values["id"])
            if kind=="api_label":
                values["label"] = name(text)
                return await self.prompt("api_sid","Pegá el Account SID de Twilio, que empieza con AC.",values)
            if kind=="api_sid":
                if not re.fullmatch(r"AC[0-9a-fA-F]{32}",text.strip()): raise ValueError("El Account SID debe empezar con AC y contener 32 caracteres hexadecimales.")
                values["account_sid"] = text.strip(); self.flow = ("api_username",values)
                return await self.screen.show("¿Vas a usar el Auth Token de la cuenta o una API Key? Si es una API Key, pegá ahora el SID que empieza con SK.",[[b("Usar Auth Token de la cuenta","apiauth","primary")],[b("Cancelar","cancel")]],"api_username")
            if kind=="api_username":
                if not re.fullmatch(r"SK[0-9a-fA-F]{32}",text.strip()): raise ValueError("Pegá un SID de API Key que empiece con SK, o elegí Auth Token.")
                values["username"] = text.strip()
                return await self.prompt("api_secret","Pegá el API Secret. Voy a borrar el mensaje del chat al recibirlo.",values)
            if kind=="api_secret":
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}",text.strip()): raise ValueError("La credencial no tiene un formato válido. Volvé a pegarla.")
                values["secret"] = text.strip()
                return await self.prompt("api_sender","Ingresá el remitente habilitado: un nombre alfanumérico de hasta 11 caracteres, un número Twilio +país o un Messaging Service SID que empiece con MG. El nombre puede reemplazarse por un número corto según el país; no se garantiza anonimato.",values)
            if kind=="api_sender":
                values["sender"] = text.strip(); values["mode"] = "live"
                validate_profile(values)
                self.flow = ("api_mode",values)
                return await self.screen.show("Elegí el tipo de credenciales. Producción puede gastar saldo al confirmar un envío. Prueba solo sirve con las credenciales de prueba oficiales de Twilio; no convierte una cuenta real en gratuita.",[[b("💳 Producción","apimode:live","primary")],[b("🧪 Credenciales de prueba","apimode:test")],[b("Cancelar","cancel","danger")]],"api_mode")
            if kind=="compose":
                channel=values.get("channel","sms")
                tts_voice=values.get("tts_voice",voice.DEFAULT_VOICE)
                if channel=="voice_text":voice.twiml(text,channel,tts_voice)
                else:text=body(text,self.settings.max_segments)
                return await self.make_draft(values["kind"],values["id"],text,channel,tts_voice)
            raise ValueError("Elegí una opción con los botones o cancelá la operación.")
        matches = suggestions(text,self.store.contacts(),self.store.groups())
        if matches:
            rows = [[b(("👥 Enviar al grupo " if m["kind"]=="group" else "✉️ Enviar a ")+m["name"],f"compose:{'g' if m['kind']=='group' else 'c'}:{m['id']}")] for m in matches]
            return await self.screen.show("<b>Quizás quisiste decir…</b>\nElegí la coincidencia correcta. Después vas a escribir y confirmar el mensaje.",rows+BACK,"suggestions")
        await self.screen.show("No encontré un contacto o grupo parecido. Podés escribir «enviar nombre» o elegir una opción.",[[b("📒 Números","contacts:0"),b("👥 Grupos","groups:0")]]+BACK,"suggestions")

    async def handle(self,update):
        if not self.authorized(update):
            log.warning("unauthorized_update_rejected")
            return
        callback = update.get("callback_query")
        try:
            if callback:
                try:
                    await self.telegram.call("answerCallbackQuery",callback_query_id=callback["id"])
                except TelegramError as exc:
                    if exc.code!=400:
                        raise
                    # A callback acknowledgement can expire while the service restarts.
                    log.info("callback_ack_expired code=400")
                if callback.get("message",{}).get("message_id")!=self.screen.message_id:
                    return
                return await self.callback(callback.get("data", ""))
            message = update["message"]
            self.store.track(self.settings.chat_id,message["message_id"])
            await self.screen.delete(message["message_id"])
            text = message.get("text","")
            if message.get('audio') or message.get('document'):
                return await self.audio_attachment(message.get('audio') or message['document'])
            if not text:
                raise ValueError("Por ahora ingresá texto o usá los botones del menú.")
            if text.startswith("/"):
                command = text.split()[0].split("@")[0].lower()
                routes = {"/start":"main","/enviar":"sendlist:0","/grupal":"sendgroups:0","/numeros":"contacts:0","/agregar":"contactadd","/grupos":"groups:0","/favoritos":"favorites:0","/apis":"apis","/estado":"apistatus","/historial":"history:0","/limpiar":"clearask","/ayuda":"help","/cancelar":"cancel"}
                routes.update({'/llamar':'calllist:0','/llamadas':'callgroups:0','/backups':'backups'})
                if command=="/detener":
                    if self.engine.current_id:
                        cid = self.engine.current_id; self.engine.stop(cid)
                        return await self.campaign_view(cid)
                    return await self.main("No hay un envío en ejecución.")
                self.flow = None
                if command in routes:
                    return await self.callback(routes[command])
                return await self.help()
            return await self.text(text)
        except ValueError as exc:
            rows = [[b("Cancelar","cancel","danger")]]+BACK if self.flow else BACK
            await self.screen.show("<b>No se pudo completar</b>\n"+h(str(exc))+"\n\n"+("Podés volver a escribir el dato o cancelar." if self.flow else "No se confirmó ningún envío nuevo."),rows,"error")
        except (KeyError,IndexError,TypeError):
            log.warning("invalid_update_rejected")
            await self.main("La opción ya no está vigente. Volvé a elegirla.")

    async def close(self):
        for task in list(self.background):
            task.cancel()
        if self.background:
            await asyncio.gather(*list(self.background),return_exceptions=True)
