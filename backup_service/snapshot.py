# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import env_read
from vault import prepare

if __name__=='__main__':
    os.umask(0o077)
    project,destination=map(Path,sys.argv[1:])
    prepare(project,env_read(project/'.env',os.getuid()),destination)
