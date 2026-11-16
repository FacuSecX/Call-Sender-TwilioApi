# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations
import hashlib,json,os,re,stat,tempfile
from pathlib import Path


class BackupError(ValueError):pass


def strict_json(data):
    try:
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,UnicodeError):raise BackupError('El archivo de backup no tiene un formato válido.') from None


def secure_read(path,limit,owner=None,mode=None):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit or (owner is not None and info.st_uid!=owner) or (mode is not None and stat.S_IMODE(info.st_mode)!=mode):
            raise BackupError('El archivo tiene permisos, tamaño o formato inseguros.')
        data=os.read(fd,limit+1)
        if len(data)>limit:raise BackupError('El archivo supera el tamaño permitido.')
        return data
    finally:os.close(fd)


def atomic_json(path,value,mode=0o600,uid=None,gid=None):
    path=Path(path)
    fd,temp=tempfile.mkstemp(prefix='.sms-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        if uid is not None:os.fchown(fd,uid,gid)
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def validate_download_url(value):
    if not isinstance(value,str) or not re.fullmatch(r'https://mega\.nz/file/[A-Za-z0-9_-]{8}#[A-Za-z0-9_-]{43}',value):
        raise BackupError('Ingresá un enlace completo de archivo MEGA, incluyendo la clave después de #.')
    return value


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def env_read(path,owner):
    raw=secure_read(path,1024*1024,owner,0o600).decode('utf-8')
    output={}
    for line in raw.splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        key,sep,value=line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*',key):raise BackupError('Configuración .env inválida.')
        try:output[key]=json.loads(value) if value.startswith('"') else value.strip("'")
        except ValueError:raise BackupError('Configuración .env inválida.') from None
        if not isinstance(output[key],str):raise BackupError('Configuración .env inválida.')
    return output


def env_write(path,values,uid,gid):
    fd,temp=tempfile.mkstemp(prefix='.restore-env-',dir=path.parent)
    try:
        os.fchmod(fd,0o600);os.fchown(fd,uid,gid)
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            for key,value in values.items():out.write(key+'='+json.dumps(str(value),ensure_ascii=False)+'\n')
            out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
