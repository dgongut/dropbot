# dropbot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/dropbot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)
[![](https://badgen.net/badge/icon/telegram?icon=telegram&label)](https://t.me/dockercontrollerbotnews)
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
- ✅ Detección de archivos de Documentos
- ✅ Detección de archivos de Fotos
- ✅ Detección de archivos de Torrent
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
| FILTER_PHOTO                   | ❌           | Especifica si los archivos de imagen deben almacenarse en una carpeta separada `/photo` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)   |
| FILTER_AUDIO                   | ❌           | Especifica si los archivos de audio deben almacenarse en una carpeta separada `/audio` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_VIDEO                   | ❌           | Especifica si los archivos de video deben almacenarse en una carpeta separada `/video` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |
| FILTER_TORRENT                 | ❌           | Especifica si los archivos de torrent deben almacenarse en una carpeta separada `/torrent` en lugar de la carpeta `/downloads`. 0 = no, 1 = sí (por defecto 0)    |

### Anotaciones
Será necesario mapear un volumen para almacenar lo que el bot escribe en /app/schedule

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
    volumes:
      - /ruta/para/descargar/general:/downloads
      #- /ruta/para/descargar/audio:/audio
      #- /ruta/para/descargar/video:/video
      #- /ruta/para/descargar/foto:/photo
      #- /ruta/para/descargar/torrent:/torrent
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
