# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations
import json,os,sqlite3,subprocess
from pathlib import Path
from common import BackupError,atomic_json,digest,secure_read,strict_json

FILES={'manifest.json','configuration.json','smsbot.sqlite3'}
TABLES={'contacts','groups_','members','campaigns','recipients','attempts','state','chat_messages'}
CONFIG_KEYS={'TWILIO_PROFILES_JSON','SEND_INTERVAL_SECONDS','RETRY_DELAY_SECONDS','RECEIPT_INTERVAL_SECONDS','MAX_MESSAGE_SEGMENTS','MAX_CAMPAIGN_RECIPIENTS','CALL_INTERVAL_SECONDS','MAX_CONCURRENT_CALLS'}


def vc(args,password=None):
    command=['/usr/bin/veracrypt','-t','--non-interactive',*args]
    if password is not None:command+=['--stdin']
    try:
        result=subprocess.run(command,input=(password+'\n').encode() if password is not None else None,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=240)
    except (OSError,subprocess.TimeoutExpired):raise BackupError('VeraCrypt no pudo completar la operación.') from None
    if result.returncode:raise BackupError('No se pudo abrir o crear la bóveda VeraCrypt. Revisá su contraseña.')


def mount(container,directory,password,readonly=False):
    args=['--pim=0','--keyfiles=','--protect-hidden=no','--fs-options=nodev,nosuid,noexec,umask=077']
    if readonly:args+=['--mount-options=ro']
    vc([*args,str(container),str(directory)],password)


def unmount(directory):vc(['--unmount',str(directory)])


def sqlite_copy(source,target):
    src=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)
    dst=sqlite3.connect(target)
    try:src.backup(dst)
    finally:dst.close();src.close()
    target.chmod(0o600)


def validate_profiles(raw):
    try:profiles=json.loads(raw)
    except (ValueError,TypeError):raise BackupError('Las APIs del backup no son válidas.') from None
    import re
    if not isinstance(profiles,list) or len(profiles)>20:raise BackupError('Las APIs del backup no son válidas.')
    ids=set()
    for p in profiles:
        if not isinstance(p,dict) or not re.fullmatch(r'[a-f0-9]{12}',str(p.get('id',''))) or p['id'] in ids:raise BackupError('Las APIs del backup no son válidas.')
        ids.add(p['id'])
        if not re.fullmatch(r'AC[0-9a-fA-F]{32}',str(p.get('account_sid',''))) or not re.fullmatch(r'(AC|SK)[0-9a-fA-F]{32}',str(p.get('username',''))) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',str(p.get('secret',''))) or p.get('mode') not in {'live','test'} or not isinstance(p.get('enabled'),bool):
            raise BackupError('Las credenciales del backup no tienen un formato válido.')
        sender=p.get('sender','');label=p.get('label','');origin=p.get('voice_sender','')
        if not isinstance(label,str) or not 1<=len(label)<=60 or any(ord(c)<32 for c in label) or not isinstance(sender,str) or not (re.fullmatch(r'MG[0-9a-fA-F]{32}',sender) or re.fullmatch(r'\+[1-9]\d{6,14}',sender) or (re.fullmatch(r'[A-Za-z0-9 ]{1,11}',sender) and any(c.isalpha() for c in sender))):raise BackupError('Nombre o remitente de API inválido.')
        if origin and not re.fullmatch(r'\+[1-9]\d{6,14}',str(origin)):raise BackupError('El origen de llamadas del backup no es válido.')
        if p['username'].startswith('AC') and p['username']!=p['account_sid']:raise BackupError('La credencial no corresponde al Account SID.')
    return profiles


def validate_database(path):
    if path.is_symlink() or path.stat().st_size>128*1024**2:raise BackupError('La base del backup no es válida.')
    con=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:
        con.execute('PRAGMA trusted_schema=OFF')
        if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise BackupError('La base del backup está dañada.')
        objects=con.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if {name for typ,name,sql in objects if typ=='table'}!=TABLES or any(typ not in {'table','index'} or (sql and 'VIRTUAL' in sql.upper()) for typ,name,sql in objects):
            raise BackupError('La base del backup contiene una estructura no admitida.')
        if con.execute('PRAGMA foreign_key_check').fetchone():raise BackupError('La base contiene referencias inválidas.')
        counts={table:con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('contacts','groups_','campaigns')}
        if counts['contacts']>50000 or counts['groups_']>50000:raise BackupError('El backup supera el límite de contactos o grupos.')
        return counts
    except sqlite3.Error:raise BackupError('No se pudo validar la base de datos del backup.') from None
    finally:con.close()


def prepare(project,env,directory):
    database=project/'data/smsbot.sqlite3'
    if database.is_symlink() or not database.is_file():raise BackupError('No se encontró la base de contactos.')
    sqlite_copy(database,directory/'smsbot.sqlite3')
    counts=validate_database(directory/'smsbot.sqlite3')
    config={k:v for k,v in env.items() if k in CONFIG_KEYS}
    validate_profiles(config.get('TWILIO_PROFILES_JSON','[]'))
    atomic_json(directory/'configuration.json',config)
    atomic_json(directory/'manifest.json',{'format':'bot-sms-twilio','version':1,'counts':counts,'hashes':{name:digest(directory/name) for name in ('configuration.json','smsbot.sqlite3')}})
    return counts


def verify(directory):
    if {p.name for p in directory.iterdir()}!=FILES:raise BackupError('El contenedor contiene archivos inesperados.')
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():raise BackupError('El backup contiene archivos no admitidos.')
    manifest=strict_json(secure_read(directory/'manifest.json',65536))
    if manifest.get('format')!='bot-sms-twilio' or manifest.get('version')!=1 or set(manifest.get('hashes',{}))!={'configuration.json','smsbot.sqlite3'}:raise BackupError('Este backup no pertenece al bot SMS.')
    if any(digest(directory/name)!=value for name,value in manifest['hashes'].items()):raise BackupError('El contenido del backup no supera la verificación.')
    config=strict_json(secure_read(directory/'configuration.json',1024*1024))
    if set(config)-CONFIG_KEYS or any(not isinstance(v,str) for v in config.values()):raise BackupError('El backup contiene configuración no permitida.')
    import math
    for key,value in config.items():
        if key!='TWILIO_PROFILES_JSON':
            try:
                number=float(value)
                if not math.isfinite(number) or number<=0:raise ValueError()
                if key in {'MAX_MESSAGE_SEGMENTS','MAX_CAMPAIGN_RECIPIENTS','MAX_CONCURRENT_CALLS'}:int(value)
            except ValueError:raise BackupError('El backup contiene límites de configuración inválidos.') from None
    validate_profiles(config.get('TWILIO_PROFILES_JSON','[]'))
    return config,validate_database(directory/'smsbot.sqlite3')


def create(container,mounted,password,source):
    size=max(32*1024**2,sum(p.stat().st_size for p in source.iterdir())+16*1024**2)
    size=((size+1024**2-1)//1024**2)*1024**2
    vc(['--create',str(container),'--size='+str(size),'--encryption=AES','--hash=SHA-512','--filesystem=exFAT','--volume-type=normal','--pim=0','--keyfiles=','--random-source=/dev/urandom'],password)
    container.chmod(0o600)
    import shutil
    try:
        mount(container,mounted,password)
        for name in FILES:shutil.copyfile(source/name,mounted/name)
    finally:
        if os.path.ismount(mounted):unmount(mounted)
    try:
        mount(container,mounted,password,True)
        verify(mounted)
    finally:
        if os.path.ismount(mounted):unmount(mounted)
