# dropbot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/dropbot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/dropbot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/dropbot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/dropbot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/dropbot/)
![Github stars](https://badgen.net/github/stars/dgongut/dropbot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/dropbot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/dropbot)
![Github last-commit](https://badgen.net/github/license/dgongut/dropbot)
![alt text](https://github.com/dgongut/pictures/blob/main/dropbot/mockup.png)

Descarga archivos directamente en tu servidor a su carpeta correspondiente

- ✅ Detección de archivos de Audio
- ✅ Detección de archivos de Vídeo
- ✅ Detección de archivos de Libros
- ✅ Detección de archivos de Fotos
- ✅ Detección de archivos de Torrent
- ✅ Descarga desde URLs (YouTube, Instagram, TikTok, Twitter, y 1800+ sitios más)
- ✅ Detección automática de tipo de contenido (video, audio, imagen)
- ✅ Descomprime automáticamente ficheros zip, tar y rar (y sus variantes)
- ✅ Soporte de idiomas (Spanish, English)

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)?

🖼️ Si deseas establecerle el icono al bot de telegram, te dejo [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/dropbot/dropbot.png) el icono en alta resolución. Solo tienes que descargarlo y mandárselo al @BotFather en la opción de BotPic.

## Configuración en config.py

| CLAVE                          | OBLIGATORIO | VALOR                                                                                   |
|---------------------------------|:------------:|-----------------------------------------------------------------------------------------|
| TELEGRAM_TOKEN                 |✅            | Token del bot |
| TELEGRAM_ADMIN                 |✅            | ChatId del administrador (se puede obtener hablándole al bot Rose escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
| TELEGRAM_API_HASH              | ✅           | Hash de la API de Telegram (obtenido al crear tu aplicación en https://my.telegram.org) |
| TELEGRAM_API_ID                | ✅           | ID de la API de Telegram (obtenido al crear tu aplicación en https://my.telegram.org)   |
| LANGUAGE                       | ✅           | Idioma del bot (por defecto "ES" para español o "EN" para inglés)                       |
| PARALLEL_DOWNLOADS             | ❌           | Número de ficheros que se transfieren a la vez (descargas/subidas simultáneas). Por defecto 2 |
| FAST_CONNECTIONS               | ❌           | Número de conexiones paralelas por fichero para acelerar la transferencia (estilo FastTelethon). 1 = desactivado (método estándar de Telethon). Recomendado 4-8. Por defecto 8 |
| FILTER_PHOTO                   | ❌           | Especifica si los archivos de imagen deben almacenarse en una carpeta separada `/photo` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)   |
| FILTER_AUDIO                   | ❌           | Especifica si los archivos de audio deben almacenarse en una carpeta separada `/audio` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_VIDEO                   | ❌           | Especifica si los archivos de video deben almacenarse en una carpeta separada `/video` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_TORRENT                 | ❌           | Especifica si los archivos de torrent deben almacenarse en una carpeta separada `/torrent` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_EBOOK                   | ❌           | Especifica si los archivos de libros electrónicos deben almacenarse en una carpeta separada `/ebook` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_URL_VIDEO               | ❌           | Especifica si los archivos de vídeo descargados desde URLs deben almacenarse en una carpeta separada `/url_video` en lugar de donde van los vídeos. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_URL_AUDIO               | ❌           | Especifica si los archivos de audio descargados desde URLs deben almacenarse en una carpeta separada `/url_audio` en lugar de donde van los audios. 0 = no, 1 = sí (por defecto 0)    |
| AUTO_DOWNLOAD_FORMAT           | ❌           | Descarga automática de URLs sin preguntar. Valores: `ASK` (preguntar, por defecto), `VIDEO` (descargar siempre como video), `AUDIO` (descargar siempre como audio)    |

### Ejemplo de Docker-Compose para su ejecución normal

```yaml
services:
  dropbot:
    environment:
      - TELEGRAM_TOKEN=
      - TELEGRAM_ADMIN=
      - TELEGRAM_API_HASH=
      - TELEGRAM_API_ID=
      - LANGUAGE=ES
      #- PARALLEL_DOWNLOADS=2
      #- FAST_CONNECTIONS=8
      #- FILTER_PHOTO=0
      #- FILTER_AUDIO=0
      #- FILTER_VIDEO=0
      #- FILTER_TORRENT=0
      #- FILTER_EBOOK=0
      #- FILTER_URL_VIDEO=0
      #- FILTER_URL_AUDIO=0
      #- AUTO_DOWNLOAD_FORMAT=ASK
    volumes:
      - /ruta/para/descargar/general:/downloads
      #- /ruta/para/descargar/audio:/audio
      #- /ruta/para/descargar/video:/video
      #- /ruta/para/descargar/foto:/photo
      #- /ruta/para/descargar/torrent:/torrent
      #- /ruta/para/descargar/ebook:/ebook
      #- /ruta/para/descargar/videos_desde_urls:/url_video
      #- /ruta/para/descargar/audios_desde_urls:/url_audio
    image: dgongut/dropbot:latest
    container_name: dropbot
    restart: always
    network_mode: host
    tty: true
```

---

## Solo para desarrolladores - Ejecución con código local

Para su ejecución en local y probar nuevos cambios de código, se necesita renombrar el fichero `.env-example` a `.env` con los valores necesarios para su ejecución.
Es necesario establecer un `TELEGRAM_TOKEN` y un `TELEGRAM_ADMIN` correctos y diferentes al de la ejecución normal.

La estructura de carpetas debe quedar:

```
dropbot/
    ├── .env
    ├── .gitignore
    ├── LICENSE
    ├── requirements.txt
    ├── README.md
    ├── config.py
    ├── dropbot.py
    ├── Dockerfile_local
    ├── docker-compose.yaml
    └── locale
        ├── en.json
        └── es.json
```

Para levantarlo habría que ejecutar en esa ruta: `docker compose up -d`

Para detenerlo y eliminarlo: `docker compose down --rmi`
