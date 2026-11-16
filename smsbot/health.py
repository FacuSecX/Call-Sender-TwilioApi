# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
import json,os,re,tempfile


def mark_ready(settings,store):
    probe=settings.data_dir/'restore-probe.json'
    if not probe.is_file() or probe.is_symlink() or probe.stat().st_size>4096:return
    try:
        value=json.loads(probe.read_text())
        ident=value.get('id','')
        if not re.fullmatch(r'[a-f0-9]{32}',ident):return
        if store.one('PRAGMA quick_check').get('quick_check')!='ok':return
        target=settings.data_dir/'restore-ready.json'
        fd,temp=tempfile.mkstemp(prefix='.ready-',dir=settings.data_dir)
        try:
            os.chmod(temp,0o600)
            with os.fdopen(fd,'w') as out:json.dump({'id':ident,'pid':os.getpid(),'ready':True},out);out.flush();os.fsync(out.fileno())
            os.replace(temp,target)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    except (OSError,ValueError,AttributeError):return
