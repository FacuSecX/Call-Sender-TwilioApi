<h1 align="center">SMS & Call Sender Telegram Bot</h1>

Bot de Telegram programable para enviar llamadas y SMS mediante la API de [Twilio](https://www.twilio.com/en-us).
<br>
<br>

<p align="center">
<img alt="sms-call-sender-bot" src="http://imgfz.com/i/cjNwakv.png" title="sms-call-sender-bo">
</p>
<br>

## Cómo funciona

El bot utiliza la API de [Twilio](https://www.twilio.com/en-us) para realizar llamadas y enviar mensajes. Permite configurar varias APIs dentro del mismo bot y utilizar las que estén disponibles para procesar las listas de números.

## Envío de mensajes

Agregá un número con el código de país, sin el signo `+`. Por ejemplo, para un celular de Argentina: `549`, seguido del código de área y del número.

Después, escribí el mensaje y confirmá el envío desde el bot.

## Envío de llamadas

Podés realizar llamadas a números individuales o a grupos de números, utilizando una o varias APIs configuradas.

<p align="center">
<img alt="call-twilioapibot" src="http://imgfz.com/i/Ip9cG6L.png" title="call-twilioapibot">
</p>
<br>

Las opciones disponibles son:

* **Llamada normal:** la llamada finaliza automáticamente cuando el destinatario atiende.
* **Lectura de texto:** escribí un texto y elegí una voz masculina o femenina. Al atender, el destinatario escuchará el mensaje y luego la llamada finalizará.
* **Reproducción de MP3:** adjuntá un archivo de audio al bot para reproducirlo cuando el destinatario atienda.

## Estado de los envios

Siempre despues de una llamada o SMS nos permite saber si el mensaje o la llamada llego al telefono o si fue atendido en caso de una llamada


## Instalación

Subí los archivos del proyecto al VPS mediante SSH/SFTP y ejecutá el instalador desde la carpeta del proyecto:

```bash
git clone https://github.com/FacuSecX/Call-Sender-TwilioApi
cd Call-Sender-TwilioApi
sudo bash instalar.sh
```

## 🤝 Autor

**Creado por** [FacuSecX](https://github.com/FacuSecX/)
