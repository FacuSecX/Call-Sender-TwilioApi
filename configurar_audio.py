# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from pathlib import Path
import sys
from dotenv import dotenv_values
from smsbot.config import atomic_env,Settings
from smsbot.media import public_base


def main():
    path=Path(__file__).resolve().parent/'.env'
    values=dict(dotenv_values(path,interpolate=False))
    choice=input('Audios: 1) Twilio Assets por HTTPS, sin abrir puertos; 2) servidor propio [1]: ').strip() or '1'
    if choice=='1':
        values.update(AUDIO_BACKEND='twilio',AUDIO_PUBLIC_URL='',AUDIO_BIND_HOST='127.0.0.1')
        atomic_env(path,values);Settings.load(path)
        print('MP3 configurado con Twilio Assets. No se necesita dominio ni abrir puertos. Twilio ofrece 10.000 solicitudes gratuitas; después aplica su tarifa por uso.')
        return
    if choice!='2':raise ValueError('Elegí una opción válida.')
    print('Twilio debe poder descargar únicamente los audios temporales desde una dirección pública.')
    url=public_base(input('URL pública del audio (ejemplo http://IP_DEL_VPS:8787 o https://audio.tudominio.com): ').strip())
    if not url:raise ValueError('No se modificó la configuración: falta la URL pública.')
    port=int(input('Puerto local del servidor de audios [8787]: ').strip() or 8787)
    if not 1024<=port<=65535:raise ValueError('Puerto inválido.')
    direct=input('¿Acceso directo al puerto, sin un proxy web? s/n [n]: ').strip().lower()=='s'
    values.update(AUDIO_BACKEND='http',AUDIO_PUBLIC_URL=url,AUDIO_BIND_HOST='0.0.0.0' if direct else '127.0.0.1',AUDIO_BIND_PORT=str(port))
    atomic_env(path,values);Settings.load(path)
    print('Configuración de MP3 guardada. Solo se publican audios confirmados mediante enlaces temporales.')


if __name__=='__main__':
    try:
        if sys.argv[1:]==['--binding']:
            settings=Settings.load(Path(__file__).resolve().parent/'.env')
            print(settings.audio_bind_host,settings.audio_bind_port)
        else:main()
    except (ValueError,EOFError,KeyboardInterrupt) as exc:
        print(str(exc) if isinstance(exc,ValueError) else 'Configuración de MP3 cancelada.');raise SystemExit(1)
