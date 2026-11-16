# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
from __future__ import annotations
import fcntl,json,os,pwd,re,secrets,shutil,signal,sqlite3,stat,subprocess,sys,tempfile,time
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import BackupError,atomic_json,digest,env_read,env_write,secure_read,strict_json,validate_download_url
from cloud import CloudAdapter,HOME
import vault

ROOT=Path('/var/lib/bot-sms-backups')
SPOOL=Path('/var/spool/bot-sms-backups')
STATUS=Path('/run/bot-sms-backups/status.json')
CONFIG=Path('/etc/bot-sms-backups/config.json')
ID=re.compile(r'^[a-f0-9]{32}$')


def open_directory(path):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:os.close(fd);raise


class Worker:
    def __init__(self,config,cloud=None):
        self.config=config
        self.account=pwd.getpwnam(config['user'])
        self.project=Path(config['project'])
        self.service=config['service']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,60}\.service',self.service) or self.account.pw_uid==0 or not self.project.is_absolute():raise BackupError('Configuración del servicio inválida.')
        self.cloud=cloud or CloudAdapter()
        self.status={}
        self.project_fd=open_directory(self.project)
        self.data_fd=os.open('data',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.project_fd)
        if any(os.fstat(fd).st_uid!=self.account.pw_uid for fd in (self.project_fd,self.data_fd)):raise BackupError('El proyecto no pertenece al usuario del bot.')
        self.fixed_project=Path('/proc/self/fd')/str(self.project_fd)
        self.fixed_data=Path('/proc/self/fd')/str(self.data_fd)

    def close(self):
        os.close(self.data_fd);os.close(self.project_fd)

    def update(self,stage,percent,message,**extra):
        self.status.update(stage=stage,percent=percent,message=message,**extra)
        atomic_json(STATUS,self.status,0o640,0,self.account.pw_gid)
        atomic_json(ROOT/'status.json',self.status)

    def credentials(self):
        env=env_read(self.fixed_project/'.env',self.account.pw_uid)
        values={k:env.get(k,'') for k in ('BACKUP_MEGA_EMAIL','BACKUP_MEGA_PASSWORD','BACKUP_VC_PASSWORD')}
        if any(not v or len(v)>4096 or any(c in v for c in '\r\n\0') for v in values.values()):raise BackupError('Faltan credenciales MEGA o contraseña de VeraCrypt en .env.')
        return env,values

    def ensure_cloud(self,secret):
        try:self.cloud.ensure_session(secret['BACKUP_MEGA_EMAIL'],secret['BACKUP_MEGA_PASSWORD'])
        except BackupError:raise BackupError('ERROR de autenticación MEGA: no se pudo reautenticar con el .env actual. Revisá cuenta, contraseña o 2FA.') from None

    def snapshot(self,job):
        dest=SPOOL/self.status['id']
        fd=open_directory(dest)
        plain=job/'plain';plain.mkdir(mode=0o700)
        try:
            if os.fstat(fd).st_uid!=self.account.pw_uid:raise BackupError('La copia local no pertenece al usuario del bot.')
            fixed=Path('/proc/self/fd')/str(fd)
            for name in vault.FILES:
                (plain/name).write_bytes(secure_read(fixed/name,128*1024**2,self.account.pw_uid,0o600))
        finally:os.close(fd)
        return plain

    def export(self,job,secret):
        self.update('snapshot',5,'Guardando contactos, grupos, APIs e historial.')
        plain=self.snapshot(job)
        vault.verify(plain)
        filename='backup-'+datetime.now().strftime('%d-%m-%Y')
        container=job/filename;mounted=job/'mounted';mounted.mkdir(mode=0o700)
        self.update('encrypt',15,'Creando bóveda VeraCrypt AES-256.')
        vault.create(container,mounted,secret['BACKUP_VC_PASSWORD'],plain)
        self.update('authenticate',35,'Autenticando con MEGA.')
        self.ensure_cloud(secret)
        if self.cloud.free_bytes()<container.stat().st_size:raise BackupError('No hay espacio en MEGA para subir y verificar la nueva copia. Se conserva la anterior.')
        remote=self.cloud.upload(container,filename,self.status['id'],lambda value:self.update('upload',40+value//3,'Subiendo copia cifrada a MEGA.'))
        self.update('verify',78,'Verificando el archivo subido.')
        if not self.cloud.verify(remote,container):raise BackupError('La copia subida no superó la verificación.')
        link=self.cloud.export_link(remote)
        state_path=ROOT/'state.json'
        previous=strict_json(secure_read(state_path,1024*1024,0,0o600)) if state_path.exists() else {}
        remote.update(verified=True,url=link,filename=filename,created=datetime.now().isoformat(timespec='seconds'))
        atomic_json(state_path,{'manifest':[remote]})
        self.cloud.commit(remote)
        retained=[]
        for old in previous.get('manifest',[]):
            if old.get('id')!=remote['id']:
                try:self.cloud.delete(old['id'])
                except BackupError:retained.append(old)
        if retained:atomic_json(state_path,{'manifest':[remote,*retained]})
        self.update('completed',100,'Backup cifrado disponible en MEGA.'+(' Quedó una copia anterior pendiente de eliminar.' if retained else ' Se conserva únicamente la última copia.'),state='completed',url=link,filename=filename)

    def download(self,url,job,secret):
        self.ensure_cloud(secret)
        dest=job/'download';dest.mkdir(mode=0o700)
        self.cloud.command(['get',validate_download_url(url),str(dest)],timeout=240)
        files=list(dest.iterdir())
        if len(files)!=1 or files[0].is_symlink() or not files[0].is_file() or files[0].stat().st_size>160*1024**2:raise BackupError('El enlace no contiene una única copia SMS válida.')
        return files[0]

    def service_command(self,action):
        result=subprocess.run(['/usr/bin/systemctl',action,self.service],capture_output=True,timeout=45)
        if result.returncode:raise BackupError('No se pudo '+action+' el servicio del bot.')

    def replace_database(self,source):
        dest=self.fixed_data/('.restore-'+self.status['id'])
        fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            os.fchown(fd,self.account.pw_uid,self.account.pw_gid)
            with os.fdopen(fd,'wb') as out,source.open('rb') as src:shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
            for suffix in ('-wal','-shm'):
                path=self.fixed_data/('smsbot.sqlite3'+suffix)
                if path.exists() or path.is_symlink():path.unlink()
            os.replace(dest,self.fixed_data/'smsbot.sqlite3')
        finally:
            if dest.exists():dest.unlink()

    def arm_probe(self):
        ident=secrets.token_hex(16)
        ready=self.fixed_data/'restore-ready.json'
        if ready.exists() or ready.is_symlink():ready.unlink()
        atomic_json(self.fixed_data/'restore-probe.json',{'id':ident},0o600,self.account.pw_uid,self.account.pw_gid)
        return ident

    def wait_ready(self,ident):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                proof=strict_json(secure_read(self.fixed_data/'restore-ready.json',4096,self.account.pw_uid,0o600))
                pid=subprocess.check_output(['/usr/bin/systemctl','show',self.service,'-p','MainPID','--value'],text=True,timeout=10).strip()
                if proof.get('id')==ident and proof.get('ready') is True and str(proof.get('pid'))==pid and pid!='0':
                    vault.validate_database(self.fixed_data/'smsbot.sqlite3')
                    return
            except (OSError,BackupError,subprocess.SubprocessError):pass
            time.sleep(.5)
        raise BackupError('El bot no confirmó el reinicio y la lectura de sus datos. Se recupera la copia anterior.')

    def clear_probe(self):
        for name in ('restore-probe.json','restore-ready.json'):
            path=self.fixed_data/name
            if path.exists() or path.is_symlink():path.unlink()

    def restore(self,source,config,job,env):
        original=job/'previous.sqlite3'
        self.update('restart',85,'Aplicando la copia y reiniciando únicamente el bot SMS.')
        self.service_command('stop')
        old_env=env
        try:
            original.write_bytes(secure_read(self.fixed_data/'smsbot.sqlite3',128*1024**2,self.account.pw_uid,0o600))
            rollback=ROOT/'rollback';rollback.mkdir(mode=0o700,exist_ok=True)
            shutil.copyfile(original,rollback/'smsbot.sqlite3')
            (rollback/'smsbot.sqlite3').chmod(0o600)
            atomic_json(rollback/'configuration.json',old_env)
            merged={**old_env,**config}
            # Telegram authorization, service identity, MEGA login and encryption key stay local.
            self.replace_database(source)
            env_write(self.fixed_project/'.env',merged,self.account.pw_uid,self.account.pw_gid)
            probe=self.arm_probe()
            self.service_command('start')
            self.wait_ready(probe)
            # Both recovery copies remain available through restart and configuration/database verification.
            # Only after these succeed is the persistent temporary rollback removed; process() then cleans the job copy.
            shutil.rmtree(rollback)
            self.clear_probe()
        except BaseException:
            self.service_command('stop')
            if original.exists():self.replace_database(original)
            env_write(self.fixed_project/'.env',old_env,self.account.pw_uid,self.account.pw_gid)
            probe=self.arm_probe()
            self.service_command('start')
            self.wait_ready(probe)
            if rollback.exists():shutil.rmtree(rollback)
            self.clear_probe()
            raise

    def import_backup(self,request,job,env,secret):
        self.update('download',10,'Descargando la copia cifrada de MEGA.')
        container=self.download(request['url'],job,secret)
        mounted=job/'mounted';mounted.mkdir(mode=0o700)
        self.update('verify',45,'Abriendo y validando la bóveda antes de cambiar datos.')
        try:
            vault.mount(container,mounted,secret['BACKUP_VC_PASSWORD'],True)
            config,counts=vault.verify(mounted)
            source=job/'restore.sqlite3';vault.sqlite_copy(mounted/'smsbot.sqlite3',source)
        finally:
            if os.path.ismount(mounted):vault.unmount(mounted)
        con=sqlite3.connect(source)
        try:
            with con:
                con.execute('PRAGMA trusted_schema=OFF')
                con.execute('DELETE FROM state');con.execute('DELETE FROM chat_messages')
                con.execute("UPDATE recipients SET status='unknown',error='Envío interrumpido antes del backup; no se repite.' WHERE status='submitting'")
                con.execute("UPDATE recipients SET status='cancelled' WHERE status='pending'")
                con.execute("UPDATE campaigns SET status='interrupted',stop=1 WHERE status IN ('queued','running','draft')")
        finally:con.close()
        self.restore(source,config,job,env)
        latest={}
        if (ROOT/'state.json').exists():
            manifest=strict_json(secure_read(ROOT/'state.json',1024*1024,0,0o600)).get('manifest',[])
            if manifest:latest={k:manifest[0][k] for k in ('url','filename') if k in manifest[0]}
        self.update('completed',100,'Backup importado. Contactos y APIs restaurados; servicio reiniciado. No se reanudan envíos.',state='completed',counts=counts,**latest)

    def process(self,request):
        if set(request)-{'id','action','url'} or not ID.fullmatch(str(request.get('id',''))) or request.get('action') not in {'export','import'}:raise BackupError('Solicitud inválida.')
        if request['action']=='import':validate_download_url(request.get('url'))
        self.status={'id':request['id'],'action':request['action'],'state':'running'}
        self.update('starting',0,'Iniciando operación de backup.')
        job=ROOT/'work'/request['id'];job.mkdir(mode=0o700)
        try:
            env,secret=self.credentials()
            if request['action']=='export':self.export(job,secret)
            else:self.import_backup(request,job,env,secret)
        except Exception as exc:
            message=str(exc) if isinstance(exc,BackupError) else 'La operación falló; revisá el servicio de backups. Los datos anteriores se conservan.'
            self.update('error',0,message,state='error')
        finally:
            mount=job/'mounted'
            if os.path.ismount(mount):
                try:vault.unmount(mount)
                except BackupError:return
            shutil.rmtree(job)
            staged=SPOOL/request['id']
            if request['action']=='export' and staged.is_dir() and not staged.is_symlink():shutil.rmtree(staged)


def main():
    os.umask(0o077)
    config=strict_json(secure_read(CONFIG,16384,0,0o600))
    lock=(ROOT/'worker.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    worker=Worker(config)
    for job in (ROOT/'work').iterdir():
        if not ID.fullmatch(job.name) or job.is_symlink() or not job.is_dir():raise BackupError('El área de trabajo contiene archivos inesperados.')
        if os.path.ismount(job/'mounted'):vault.unmount(job/'mounted')
        shutil.rmtree(job)
    previous=ROOT/'status.json'
    if previous.exists():
        saved=strict_json(secure_read(previous,65536,0,0o600))
        if saved.get('state')=='running':saved.update(state='error',message='La operación se interrumpió. Se conserva la última copia verificada.')
        atomic_json(STATUS,saved,0o640,0,worker.account.pw_gid)
    try:
        while True:
            for path in sorted(SPOOL.glob('*.json')):
                if not ID.fullmatch(path.stem):continue
                try:
                    request=strict_json(secure_read(path,8192,worker.account.pw_uid,0o600))
                    if request.get('id')!=path.stem:raise BackupError('Solicitud inválida.')
                    path.unlink()
                    worker.process(request)
                except (BackupError,OSError):
                    if path.exists() and not path.is_symlink():path.unlink()
            time.sleep(1)
    finally:worker.close()


if __name__=='__main__':main()
