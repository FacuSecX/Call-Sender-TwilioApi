# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 40 * 60
TOKEN = re.compile(r"[0-9a-f]{64}")


def public_base(value):
    value = str(value or '').rstrip('/')
    if not value:return ''
    p = urlsplit(value)
    try:port = p.port
    except ValueError:raise ValueError('La dirección de audios no es válida.') from None
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.path or p.query or p.fragment or (port is not None and not 1 <= port <= 65535):
        raise ValueError('Configurá AUDIO_PUBLIC_URL con una dirección HTTP o HTTPS, sin rutas ni credenciales.')
    return value


class Media:
    def __init__(self, root, base='', assets=None):
        self.root = Path(root)
        self.base = public_base(base)
        self.assets = assets

    @property
    def enabled(self):return bool(self.base or self.assets)

    async def play_url(self, token, campaign, profile):
        if self.assets:return await self.assets.publish(self,token,campaign,profile)
        return self.url(token,campaign)

    def paths(self, token):
        if not isinstance(token,str) or not TOKEN.fullmatch(token):raise ValueError('El audio no es válido.')
        return self.root/(token+'.mp3'), self.root/(token+'.json')

    def metadata(self, token):
        audio, meta = self.paths(token)
        try:
            if audio.is_symlink() or meta.is_symlink() or not audio.is_file() or not meta.is_file() or meta.stat().st_size > 32768:raise ValueError()
            value = json.loads(meta.read_text(encoding='utf-8'))
            if not isinstance(value,dict) or not 0 < audio.stat().st_size <= MAX_BYTES or not math.isfinite(float(value['expires'])):raise ValueError()
            return value
        except (OSError,ValueError,KeyError,TypeError):
            raise ValueError('El audio temporal ya no está disponible. Adjuntá el MP3 nuevamente.') from None

    def write(self, token, value):
        _, target = self.paths(token)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        temp = self.root/('.'+secrets.token_hex(16)+'.tmp')
        try:
            with open(temp,'x',encoding='utf-8') as out:
                os.chmod(temp,0o600);json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
            os.replace(temp,target)
        finally:
            temp.unlink(missing_ok=True)

    def inspect(self, path):
        if not shutil.which('ffprobe'):raise ValueError('Falta ffprobe en el servidor. Instalá el paquete ffmpeg para usar MP3.')
        try:
            p = subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file','-show_format','-show_streams','-of','json',str(path)],capture_output=True,timeout=15,check=True)
            value = json.loads(p.stdout)
            streams = value['streams']
            seconds = float(value['format']['duration'])
            audio=[s for s in streams if s.get('codec_type')=='audio']
            if value['format']['format_name'] != 'mp3' or len(audio)!=1 or audio[0].get('codec_name')!='mp3' or any(s.get('codec_type')!='audio' and not s.get('disposition',{}).get('attached_pic') for s in streams) or not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:raise ValueError()
            return seconds
        except (OSError,subprocess.SubprocessError,ValueError,TypeError,KeyError):
            raise ValueError('Adjuntá un MP3 válido de hasta 40 minutos, sin video.') from None

    def original(self, token):
        self.metadata(token)
        source, _ = self.paths(token)
        return source, 'mp3', 'audio/mpeg'

    def telephony(self, token):
        source,_=self.paths(token)
        target=self.root/(token+'.wav')
        if target.is_symlink():raise ValueError('El archivo preparado no es válido.')
        if target.exists():return target,'wav','audio/wav'
        temp=self.root/(token+'.wav.part')
        try:
            if not shutil.which('ffmpeg'):raise ValueError('Falta ffmpeg para preparar el audio de la llamada.')
            command=['ffmpeg','-nostdin','-v','error','-threads','1','-filter_threads','1','-protocol_whitelist','file','-f','mp3','-i',str(source),
                     '-map','0:a:0','-vn','-map_metadata','-1','-ac','1','-ar','8000','-c:a','pcm_mulaw','-fs',str(MAX_BYTES),'-f','wav','-y',str(temp)]
            subprocess.run(command,capture_output=True,check=True,timeout=90)
            if not 0<temp.stat().st_size<=MAX_BYTES:raise ValueError()
            probe=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(temp)],capture_output=True,check=True,timeout=15)
            value=json.loads(probe.stdout);streams=value['streams'];seconds=float(value['format']['duration'])
            if len(streams)!=1 or streams[0].get('codec_name')!='pcm_mulaw' or streams[0].get('sample_rate')!='8000' or streams[0].get('channels')!=1 or not math.isfinite(seconds) or not 0<seconds<=MAX_SECONDS:raise ValueError()
            temp.chmod(0o600);os.replace(temp,target)
            return target,'wav','audio/wav'
        except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):
            raise ValueError('No se pudo preparar el MP3 para telefonía. No se inició ninguna llamada.') from None
        finally:temp.unlink(missing_ok=True)

    def library(self):
        values=[]
        if self.root.is_dir():
            for path in self.root.glob('*.json'):
                if not TOKEN.fullmatch(path.stem):continue
                try:
                    value=self.metadata(path.stem)
                    if value.get('reusable') and not value.get('delete_requested') and not value.get('delete_pending'):
                        values.append({'token':path.stem,'name':value['name'],'seconds':value['seconds'],'published':bool(value.get('assets')),'sha256':value.get('sha256','')})
                except ValueError:continue
        return sorted(values,key=lambda v:(v['name'].casefold(),v['token']))

    def saved(self, alias):
        if not isinstance(alias,str) or not re.fullmatch(r'[0-9a-f]{12}',alias):raise ValueError('El audio guardado no es válido.')
        matches=[v for v in self.library() if v['token'].startswith(alias)]
        if len(matches)!=1:raise ValueError('El audio guardado ya no está disponible.')
        return matches[0]

    def set_reusable(self, token, enabled):
        if not self.assets:raise ValueError('Los audios reutilizables requieren Twilio Assets.')
        value=self.metadata(token)
        if value.get('delete_pending') or value.get('delete_requested'):raise ValueError('Ese audio está pendiente de eliminación.')
        if enabled and not value.get('reusable') and len(self.library())>=20:raise ValueError('Podés conservar hasta 20 audios. Eliminá uno para guardar otro.')
        value['reusable']=bool(enabled);self.write(token,value)

    def forget(self, token):
        value=self.metadata(token);value.update(reusable=False,delete_requested=True);self.write(token,value)

    async def receive(self, telegram, attachment):
        if not self.enabled:raise ValueError('Las llamadas con MP3 todavía no están configuradas en este servidor.')
        filename=attachment.get('file_name') or ('audio.mp3' if attachment.get('mime_type')=='audio/mpeg' else '')
        if not isinstance(filename,str) or not filename.lower().endswith('.mp3'):raise ValueError('Adjuntá el archivo .mp3 como audio o documento de Telegram.')
        size=attachment.get('file_size')
        if size is not None and (not isinstance(size,int) or not 0 < size <= MAX_BYTES):raise ValueError('El MP3 debe ocupar como máximo 20 MB.')
        token=secrets.token_hex(32);audio,_=self.paths(token)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        try:
            await telegram.download(attachment['file_id'],audio,MAX_BYTES)
            if not 0 < audio.stat().st_size <= MAX_BYTES:raise ValueError('El MP3 supera el tamaño permitido.')
            import asyncio
            seconds=await asyncio.to_thread(self.inspect,audio)
            def checksum():
                result=hashlib.sha256()
                with open(audio,'rb') as source:
                    for chunk in iter(lambda:source.read(65536),b''):result.update(chunk)
                return result.hexdigest()
            fingerprint=await asyncio.to_thread(checksum)
            for saved in self.library():
                if saved.get('sha256')==fingerprint:
                    self.remove(token)
                    return saved['token'],self.metadata(saved['token'])
            name=Path(filename.replace('\\','/')).name
            name=''.join(c for c in name if ord(c)>=32)[:100] or 'audio.mp3'
            self.write(token,{'name':name,'seconds':seconds,'expires':time.time()+1800,'published':False,'campaign':'','sha256':fingerprint})
            return token,self.metadata(token)
        except BaseException:
            self.remove(token)
            raise

    def attach(self, token, campaign):
        value=self.metadata(token)
        if value.get('delete_requested') or value.get('delete_pending'):raise ValueError('El audio está pendiente de eliminación.')
        if not value.get('reusable') and (value['expires'] <= time.time() or value.get('campaign') not in ('',campaign)):raise ValueError('El audio temporal no corresponde a este envío.')
        value.update(campaign=campaign,expires=time.time()+1800);self.write(token,value)

    def activate(self, token, campaign):
        value=self.metadata(token)
        if value.get('campaign')!=campaign or value['expires']<=time.time() or value.get('delete_requested') or value.get('delete_pending'):raise ValueError('El audio venció o no corresponde al envío. Adjuntalo nuevamente.')
        value.update(published=True,expires=time.time()+1800);self.write(token,value)

    def url(self, token, campaign):
        value=self.metadata(token)
        if not self.base or not value.get('published') or value.get('campaign')!=campaign or value['expires']<=time.time():raise ValueError('El audio de la llamada ya no está disponible.')
        return self.base+'/media/'+token+'.mp3'

    def remove(self, token):
        try:
            value=self.metadata(token)
            if value.get('assets'):
                value.update(delete_pending=True,published=False,expires=0)
                self.write(token,value)
                return
        except ValueError:pass
        for path in (*self.paths(token),self.root/(token+'.wav'),self.root/(token+'.wav.part')):path.unlink(missing_ok=True)

    def cleanup(self, store):
        if not self.root.is_dir():return
        for path in self.root.glob('*.json'):
            token=path.stem
            if not TOKEN.fullmatch(token):continue
            try:
                value=self.metadata(token);cid=value.get('campaign')
                campaign=store.campaign(cid) if cid else None
                pending=store.one("SELECT COUNT(*) n FROM recipients r JOIN campaigns c ON c.id=r.campaign_id WHERE c.audio_token=? AND (r.status IN ('submitting','accepted','queued','ringing','in-progress') OR (r.status='pending' AND c.status IN ('queued','running')))",(token,))['n']
                if pending:
                    if value['expires']<time.time()+MAX_SECONDS:
                        value['expires']=time.time()+MAX_SECONDS+1800;self.write(token,value)
                    continue
                if value.get('reusable') and not value.get('delete_requested'):continue
                uncertain=store.one("SELECT COUNT(*) n FROM recipients r JOIN campaigns c ON c.id=r.campaign_id WHERE c.audio_token=? AND r.status='unknown'",(token,))['n']
                if uncertain and value['expires']>time.time():continue
                if value.get('delete_requested') or (campaign and campaign['status']!='draft') or value['expires']<=time.time():self.remove(token)
            except ValueError:self.remove(token)
        for path in self.root.glob('*.mp3'):
            if TOKEN.fullmatch(path.stem) and not path.with_suffix('.json').exists() and time.time()-path.stat().st_mtime>1800:path.unlink(missing_ok=True)
        for path in self.root.glob('*.wav'):
            if TOKEN.fullmatch(path.stem) and not path.with_suffix('.json').exists() and time.time()-path.stat().st_mtime>1800:path.unlink(missing_ok=True)


class AudioHTTPServer:
    def __init__(self, media, host='127.0.0.1', port=8787):
        self.media=media;self.host=host;self.port=port;self.server=None

    def start(self):
        media=self.media
        class Handler(BaseHTTPRequestHandler):
            server_version='Audio'
            sys_version=''
            def log_message(self,*args):pass
            def do_HEAD(self):self.serve(False)
            def do_GET(self):self.serve(True)
            def serve(self,body):
                match=re.fullmatch(r'/media/([0-9a-f]{64})\.mp3',self.path)
                if not match:return self.send_error(404)
                try:
                    value=media.metadata(match[1])
                    if not value.get('published') or value['expires']<=time.time():return self.send_error(404)
                    path,_=media.paths(match[1])
                    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    with os.fdopen(fd,'rb') as source:
                        info=os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=MAX_BYTES:return self.send_error(404)
                        start,end=0,info.st_size-1;partial=False
                        if self.headers.get('Range'):
                            r=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
                            if not r:return self.send_error(416)
                            start=int(r[1]);end=min(int(r[2]) if r[2] else end,end);partial=True
                            if start>end:return self.send_error(416)
                        self.send_response(206 if partial else 200)
                        self.send_header('Content-Type','audio/mpeg');self.send_header('Content-Length',str(end-start+1))
                        self.send_header('Cache-Control','no-store');self.send_header('Accept-Ranges','bytes');self.send_header('X-Content-Type-Options','nosniff')
                        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{info.st_size}')
                        self.end_headers()
                        if body:
                            source.seek(start);remaining=end-start+1
                            while remaining:
                                chunk=source.read(min(65536,remaining))
                                if not chunk:break
                                self.wfile.write(chunk);remaining-=len(chunk)
                except (OSError,ValueError):
                    if not self.wfile.closed:
                        try:self.send_error(404)
                        except OSError:pass
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            slots=threading.BoundedSemaphore(16)
            def process_request(self,request,address):
                if not self.slots.acquire(blocking=False):return self.shutdown_request(request)
                try:super().process_request(request,address)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,request,address):
                try:super().process_request_thread(request,address)
                finally:self.slots.release()
            def get_request(self):
                sock,address=super().get_request();sock.settimeout(10);return sock,address
        self.server=Server((self.host,self.port),Handler)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def close(self):
        if self.server:self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
