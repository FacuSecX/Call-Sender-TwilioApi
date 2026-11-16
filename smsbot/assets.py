# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations

import asyncio
import hashlib
import re
import time

import httpx


class AssetsError(ValueError):
    pass


def sid(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix+r'[0-9a-fA-F]{32}', value):
        raise AssetsError('Twilio Assets devolvió un identificador inválido. No se inició la llamada.')
    return value


class TwilioAssets:
    api = 'https://serverless.twilio.com/v1/Services'
    upload_api = 'https://serverless-upload.twilio.com/v1/Services'

    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10, write=120), follow_redirects=False)
        self.owns_client = client is None
        self.lock = asyncio.Lock()

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def request(self, method, url, profile, *, missing=False, **kwargs):
        try:
            response = await self.client.request(method, url, auth=httpx.BasicAuth(profile['username'], profile['secret']), **kwargs)
        except httpx.RequestError:
            raise AssetsError('No se pudo preparar el MP3 en Twilio Assets. No se inició la llamada.') from None
        if missing and response.status_code == 404:
            return None
        if not response.is_success:
            try:
                code = str(response.json().get('code') or response.status_code)
            except (ValueError, AttributeError):
                code = str(response.status_code)
            if not re.fullmatch(r'\d{3,8}', code):
                code = str(response.status_code)
            raise AssetsError(f'Twilio Assets rechazó la operación ({code}). Revisá los permisos de la API; no se inició la llamada.')
        if response.status_code == 204:
            return {}
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise AssetsError('La respuesta de Twilio Assets no es válida. No se inició la llamada.') from None

    @staticmethod
    def owned(service, account, name):
        return service.get('account_sid') == account and service.get('unique_name') == name

    async def publish(self, media, token, campaign, profile):
        async with self.lock:
            value = media.metadata(token)
            if not value.get('published') or value.get('campaign') != campaign or value['expires'] <= time.time() or value.get('delete_pending'):
                raise AssetsError('El audio ya no está disponible. Adjuntalo nuevamente.')
            account = sid(profile.get('account_sid'), 'AC')
            if profile.get('mode') != 'live':
                raise AssetsError('Twilio Assets necesita credenciales de producción.')
            entries = value.setdefault('assets', {})
            state = entries.setdefault(account, {'name': 'sms-audio-'+token[:24]})
            name = 'sms-audio-'+token[:24]
            if state.get('name') != name:
                raise AssetsError('La referencia del audio no corresponde a este envío.')
            def save_state():
                current=media.metadata(token)
                current.setdefault('assets',{})[account]=state
                media.write(token,current)
            save_state()
            if state.get('url') and state.get('format','mp3') == 'mp3':
                return self.url(state['domain'], token,'mp3')
            if state.get('format') == 'wav':
                previous_version = state.get('version')
                state = {'name': name, 'format': 'mp3', **{
                    key: state[key] for key in ('service', 'environment', 'domain') if state.get(key)}}
                if previous_version:
                    state['legacy_versions'] = [sid(previous_version, 'ZN')]
                entries[account] = state
                save_state()
            try:
                path,extension,mime=media.original(token)
            except ValueError as exc:raise AssetsError(str(exc)) from None
            state['format']=extension;save_state()
            service = await self.request('GET', self.api+'/'+name, profile, missing=True)
            if service is None:
                service = await self.request('POST', self.api, profile, data={
                    'UniqueName': name, 'FriendlyName': 'Bot SMS call audio', 'IncludeCredentials': 'false', 'UiEditable': 'false'})
            if not self.owned(service, account, name):
                raise AssetsError('Se conservó un servicio de Twilio que no pertenece a este audio.')
            state['service'] = sid(service.get('sid'), 'ZS');save_state()
            base = self.api+'/'+state['service']
            if not state.get('environment'):
                env = await self.request('GET', base+'/Environments/playback', profile, missing=True)
                if env is None:
                    env = await self.request('POST', base+'/Environments', profile, data={'UniqueName': 'playback', 'DomainSuffix': 'play'})
                state.update(environment=sid(env.get('sid'), 'ZE'), domain=self.domain(env.get('domain_name')))
                save_state()
            if not state.get('asset'):
                asset = await self.request('POST', base+'/Assets', profile, data={'FriendlyName': 'Call audio'})
                state['asset'] = sid(asset.get('sid'), 'ZH');save_state()
            if not state.get('version'):
                with open(path, 'rb') as audio:
                    version = await self.request('POST', self.upload_api+'/'+state['service']+'/Assets/'+state['asset']+'/Versions', profile,
                        data={'Path': '/media/'+token+'.'+extension, 'Visibility': 'public'}, files={'Content': ('audio.'+extension, audio, mime)})
                state['version'] = sid(version.get('sid'), 'ZN');save_state()
            if not state.get('build'):
                versions = [state['version'], *state.get('legacy_versions', [])]
                build = await self.request('POST', base+'/Builds', profile, data={'AssetVersions': versions})
                state['build'] = sid(build.get('sid'), 'ZB');save_state()
            deadline = time.monotonic()+180
            while True:
                build = await self.request('GET', base+'/Builds/'+sid(state['build'], 'ZB'), profile)
                if build.get('status') == 'completed':
                    break
                if build.get('status') == 'failed' or time.monotonic() >= deadline:
                    raise AssetsError('Twilio no completó la preparación del MP3. No se inició la llamada.')
                await asyncio.sleep(2)
            await self.request('POST', base+'/Environments/'+sid(state['environment'], 'ZE')+'/Deployments', profile, data={'BuildSid': state['build']})
            url = self.url(state['domain'], token,extension)
            expected_size=path.stat().st_size
            expected_hash=await asyncio.to_thread(lambda:hashlib.sha256(path.read_bytes()).digest())
            deadline = time.monotonic()+getattr(self,'prepare_timeout',40)
            while True:
                ready = False
                try:
                    async with self.client.stream('GET', url, follow_redirects=False) as response:
                        if response.status_code==200 and response.headers.get('content-type', '').split(';')[0] in {'audio/mpeg','audio/mp3','audio/wav','audio/x-wav','audio/wave'}:
                            size=0;checksum=hashlib.sha256()
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                size+=len(chunk)
                                if size>expected_size:break
                                checksum.update(chunk)
                            ready=size==expected_size and checksum.digest()==expected_hash
                except httpx.RequestError:
                    pass
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise AssetsError('El MP3 todavía no está disponible por HTTPS. No se inició la llamada.')
                await asyncio.sleep(2)
            state['url'] = url;save_state()
            return url

    @staticmethod
    def domain(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9-]{1,63}\.twil\.io', value):
            raise AssetsError('El dominio de Twilio Assets no es válido.')
        return value

    @classmethod
    def url(cls, domain, token, extension='mp3'):
        if not re.fullmatch(r'[0-9a-f]{64}', token) or extension not in {'mp3','wav'}:
            raise AssetsError('El identificador de audio no es válido.')
        return 'https://'+cls.domain(domain)+'/media/'+token+'.'+extension

    async def delete(self, state, token, profile):
        account = sid(profile.get('account_sid'), 'AC')
        name = 'sms-audio-'+token[:24]
        if state.get('name') != name:
            raise AssetsError('La referencia de limpieza del audio no es válida.')
        service = await self.request('GET', self.api+'/'+name, profile, missing=True)
        if service is None:
            return
        if not self.owned(service, account, name) or (state.get('service') and state['service'] != service.get('sid')):
            raise AssetsError('Se conservó un servicio de Twilio ajeno a este audio.')
        await self.request('DELETE', self.api+'/'+sid(service.get('sid'), 'ZS'), profile)

    async def cleanup(self, media, profiles):
        if not media.root.is_dir():
            return
        async with self.lock:
            for path in media.root.glob('*.json'):
                if not re.fullmatch(r'[0-9a-f]{64}', path.stem):
                    continue
                try:
                    value = media.metadata(path.stem)
                    if not value.get('delete_pending') or value.get('cleanup_after', 0) > time.time():
                        continue
                    for account, state in list(value.get('assets', {}).items()):
                        candidates = [p for p in profiles.all() if p.get('account_sid') == account and p.get('mode') == 'live']
                        for profile in candidates:
                            try:
                                await self.delete(state, path.stem, profile)
                                del value['assets'][account]
                                media.write(path.stem, value)
                                break
                            except AssetsError:
                                continue
                    if not value.get('assets'):
                        media.remove(path.stem)
                    else:
                        value['cleanup_after'] = time.time()+300; media.write(path.stem, value)
                except (ValueError, OSError):
                    continue
