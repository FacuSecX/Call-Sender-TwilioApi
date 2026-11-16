# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations
import json,os,re,secrets,subprocess
from pathlib import Path

SPOOL=Path('/var/spool/bot-sms-backups')
STATUS=Path('/run/bot-sms-backups/status.json')


def download_url(value):
    if not isinstance(value,str) or not re.fullmatch(r'https://mega\.nz/file/[A-Za-z0-9_-]{8}#[A-Za-z0-9_-]{43}',value.strip()):
        raise ValueError('Ingresá el enlace completo del archivo MEGA, con la clave después de #.')
    return value.strip()


class Backups:
    def __init__(self,spool=SPOOL,status=STATUS,project=None):self.spool,self.status_file,self.project=spool,status,Path(project or Path.cwd())

    def status(self):
        try:
            if self.status_file.is_symlink() or self.status_file.stat().st_size>65536:return {}
            value=json.loads(self.status_file.read_text(encoding='utf-8'))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):return {}

    def request(self,action,url=None):
        if action not in {'export','import'}:raise ValueError('Operación de backup inválida.')
        if self.status().get('state')=='running' or list(self.spool.glob('*.json')):raise ValueError('Ya hay una operación de backup en curso.')
        if not self.spool.is_dir() or not os.access(self.spool,os.W_OK):raise ValueError('Los backups no están configurados. Ejecutá instalar-backups.sh en el VPS.')
        if os.name=='posix' and subprocess.run(['/usr/bin/systemctl','is-active','--quiet','bot-sms-backups.service'],capture_output=True,timeout=5).returncode:
            raise ValueError('El servicio de backups no está activo en el VPS.')
        ident=secrets.token_hex(16)
        values={'id':ident,'action':action}
        if action=='import':values['url']=download_url(url)
        snapshot=self.spool/ident
        if action=='export':
            snapshot.mkdir(mode=0o700)
            try:
                script=Path('/usr/local/lib/bot-sms-backups/snapshot.py')
                result=subprocess.run(['/usr/bin/python3','-B',str(script),str(self.project),str(snapshot)],capture_output=True,timeout=90)
                if result.returncode:raise ValueError('No se pudo preparar una copia consistente de contactos y APIs.')
            except BaseException:
                import shutil
                shutil.rmtree(snapshot)
                raise
        temp=self.spool/(ident+'.tmp')
        fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as out:json.dump(values,out);out.flush();os.fsync(out.fileno())
            os.replace(temp,self.spool/(ident+'.json'))
        finally:
            if temp.exists():temp.unlink()
        return ident
