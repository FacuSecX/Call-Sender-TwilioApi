# ==========================================
# CallerSend-TwilioApi
# https://github.com/FacuSecX/
# ==========================================
import getpass,secrets
from pathlib import Path
from dotenv import dotenv_values
from smsbot.config import atomic_env


def main():
    path=Path(__file__).resolve().parent/'.env'
    values=dict(dotenv_values(path,interpolate=False))
    email=input('Correo MEGA (vacío conserva el actual): ').strip()
    password=getpass.getpass('Contraseña MEGA (vacío conserva la actual): ')
    encryption=getpass.getpass('Contraseña VeraCrypt (vacío conserva o genera una nueva): ')
    if email:values['BACKUP_MEGA_EMAIL']=email
    if password:values['BACKUP_MEGA_PASSWORD']=password
    values['BACKUP_VC_PASSWORD']=encryption or values.get('BACKUP_VC_PASSWORD') or secrets.token_urlsafe(32)
    if not values.get('BACKUP_MEGA_EMAIL') or not values.get('BACKUP_MEGA_PASSWORD'):
        raise ValueError('Faltan credenciales MEGA; no se modificó .env.')
    atomic_env(path,values)
    print('Credenciales privadas guardadas en .env. Conservá la contraseña VeraCrypt para restaurar en otro servidor.')


if __name__=='__main__':main()
