# dropbot
[![](https://badgen.net/badge/icon/github?icon=github&label)](https://github.com/dgongut/dropbot)
[![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)
[![Docker Pulls](https://badgen.net/docker/pulls/dgongut/dropbot?icon=docker&label=pulls)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Stars](https://badgen.net/docker/stars/dgongut/dropbot?icon=docker&label=stars)](https://hub.docker.com/r/dgongut/dropbot/)
[![Docker Image Size](https://badgen.net/docker/size/dgongut/dropbot?icon=docker&label=image%20size)](https://hub.docker.com/r/dgongut/dropbot/)
![Github stars](https://badgen.net/github/stars/dgongut/dropbot?icon=github&label=stars)
![Github forks](https://badgen.net/github/forks/dgongut/dropbot?icon=github&label=forks)
![Github last-commit](https://img.shields.io/github/last-commit/dgongut/dropbot)
![Github license](https://badgen.net/github/license/dgongut/dropbot)

<h3 align="center">
  ReadMe en Español
  <span> | </span>
  <a href="./README_EN.md">ReadMe in English</a>
</h3>

![alt text](https://github.com/dgongut/pictures/blob/main/dropbot/mockup.png)

Descarga archivos directamente en tu servidor a su carpeta correspondiente

- ✅ Mándale un fichero y lo guarda en su carpeta: vídeo, audio (también notas de voz), fotos, torrents, libros o el resto
- ✅ Mándale un enlace de YouTube, Instagram, TikTok, Twitter y 1800+ sitios más y descarga el vídeo o el audio
- ✅ Los enlaces directos a un fichero (un PDF, un ZIP...) se descargan tal cual
- ✅ Elige la calidad de lo que bajas de un enlace: resolución máxima, MP3 o M4A sin recodificar, y título, artista y carátula dentro del audio
- ✅ Quita patrocinios y autopromoción de los vídeos de YouTube con SponsorBlock
- ✅ Playlists enteras o solo el primer vídeo, con un tope de vídeos y un tamaño máximo por descarga
- ✅ Te devuelve a Telegram lo descargado, convertido solo si Telegram no puede reproducirlo, y con aceleración por hardware (VAAPI, NVENC, QSV)
- ✅ Transferencias rápidas con varias conexiones por fichero
- ✅ `/list` y `/manage` para ver, renombrar, borrar, descomprimir (zip, tar y rar) y reenviar a Telegram lo que hay en el servidor
- ✅ Todo se ajusta desde el propio bot con `/settings`, sin tocar el docker-compose ni reiniciar
- ✅ Cada tipo de fichero a su carpeta con solo montarla
- ✅ Español e inglés

¿Lo buscas en [![](https://badgen.net/badge/icon/docker?icon=docker&label)](https://hub.docker.com/r/dgongut/dropbot)?

🖼️ Si deseas establecerle el icono al bot de telegram, te dejo [aquí](https://raw.githubusercontent.com/dgongut/pictures/main/dropbot/dropbot.png) el icono en alta resolución. Solo tienes que descargarlo y mandárselo al @BotFather en la opción de BotPic.

## Puesta en marcha

```yaml
services:
  dropbot:
    environment:
      - TELEGRAM_TOKEN=
      - TELEGRAM_ADMIN=
      - TELEGRAM_API_HASH=
      - TELEGRAM_API_ID=
    volumes:
      - /ruta/para/la/configuracion:/config
      - /ruta/para/descargar/general:/downloads
    image: dgongut/dropbot:latest
    container_name: dropbot
    restart: always
    network_mode: host
```

```bash
docker compose up -d
```

Abre Telegram, envíale `/start` a tu bot y mándale un fichero o un enlace. Todo lo demás se ajusta desde `/settings`.

> [!WARNING]
> Mapea siempre un volumen en `/config`: ahí se guardan los ajustes. Sin él se pierden al recrear el contenedor, y el bot te lo recuerda al arrancar.

| CLAVE                          | OBLIGATORIO | VALOR |
|---------------------------------|:------------:|-------|
| TELEGRAM_TOKEN                 |✅            | Token del bot |
| TELEGRAM_ADMIN                 |✅            | ChatId del administrador (se puede obtener hablándole al bot Rose escribiendo /id). Admite múltiples administradores separados por comas. Por ejemplo 12345,54431,55944 |
| TELEGRAM_API_HASH              |✅            | Hash de la API de Telegram (obtenido al crear tu aplicación en https://my.telegram.org) |
| TELEGRAM_API_ID                |✅            | ID de la API de Telegram (obtenido al crear tu aplicación en https://my.telegram.org)   |
| TELEMETRY                      |❌            | `false` para desactivar las estadísticas anónimas sin pasar por `/settings`. Lo normal es desactivarlas desde `/settings`; esto es para quien prefiera dejarlo fijado en el compose |

Aquí solo quedan las variables que el bot necesita **antes** de poder leer sus propios ajustes: cómo llegar a Telegram y quién puede hablarle. Si ponerla mal te puede dejar sin acceso al bot, va en el docker-compose, porque lo que impide que el chat funcione no se puede arreglar desde el chat.

## Comandos

| COMANDO | QUÉ HACE |
|:------------- | :-------------|
| `/start` | Saludo y cómo se usa |
| `/list` | Lo que hay en el servidor, por categorías. `/list video` (o `audio`, `photo`, `torrent`, `ebook`) va directo a una |
| `/manage` | Lo mismo, pero cada elemento abre sus acciones: renombrar, borrar, descomprimir o enviarlo a Telegram |
| `/settings` | Los ajustes del bot |
| `/version` | La versión instalada |
| `/donate` · `/donors` | Cómo apoyar el proyecto, y quién lo ha apoyado ya |

Para descargar no hace falta ningún comando: basta con mandarle el fichero o el enlace.

## Ajustes y más

<details>
<summary>⚙️ Ajustes de <code>/settings</code></summary>

Se guardan en `settings.json`, dentro de `/config`, y se aplican al momento, sin reiniciar el contenedor.

| AJUSTE | VALOR |
|:------------- | :-------------|
| Idioma | Español o English. Por defecto español |
| Enlaces → Descargar como | Qué hacer al recibir un enlace: preguntar si quieres vídeo o audio (por defecto), o descargar siempre como vídeo o siempre como audio |
| Enlaces → Tras descargar | Qué hacer con lo descargado desde un enlace, incluidas las playlists completas: preguntar (por defecto), enviar y guardar, enviar y borrar del servidor, o solo guardar. No afecta a los ficheros que envías al bot desde Telegram. Los de más de 2 GB no caben en Telegram y se quedan solo en el servidor. En vídeos largos se sigue ofreciendo cancelar la conversión o enviar el original |
| Enlaces → Calidad → Vídeo | Resolución máxima: máxima disponible (por defecto), 2160p, 1440p, 1080p, 720p, 480p o 360p. Cuenta el lado corto, así que un vídeo vertical de 1080×1920 es 1080p. Si un vídeo no tiene nada por debajo del límite se baja el más pequeño que haya |
| Enlaces → Calidad → Preferir compatible con Telegram | Pide el vídeo en H.264 y el audio en AAC, que Telegram reproduce tal cual: el vídeo se envía en segundos en vez de pasar por la conversión. Como YouTube no ofrece H.264 por encima de 1080p, con esto activo un vídeo 4K se baja en 1080p. Por defecto activado |
| Enlaces → Calidad → Formato del audio | MP3 (por defecto) o M4A, que guarda el audio AAC tal como viene, sin recodificar: suena igual que el original, pesa menos y tarda menos |
| Enlaces → Calidad → Calidad del MP3 | Estándar (~128 kbps, por defecto), alta (192 kbps) o máxima (~245 kbps). Solo con MP3 |
| Enlaces → Calidad → Etiquetas y carátula | Mete en el audio el título, el artista y la miniatura del vídeo como portada. Por defecto activado |
| Enlaces → Playlists | Al recibir una playlist: preguntar si la quieres entera o solo el primer vídeo (por defecto), o hacer siempre lo mismo. Y un tope de vídeos al bajarla entera (10, 25, 50, 100 o 200; por defecto sin tope) |
| Enlaces → SponsorBlock | Quita de los vídeos de YouTube los tramos marcados por la comunidad de [SponsorBlock](https://sponsor.ajay.app): desactivado (por defecto), patrocinios, patrocinios y autopromoción, o todo lo que no es contenido (también intros, despedidas, avances, relleno y lo que no es música en los vídeos musicales) |
| Enlaces → Tamaño máximo | Lo que pase de este tamaño no se descarga, y el bot te lo dice: 512 MB, 1, 2, 5 o 10 GB, o sin límite (por defecto). En una playlist se salta ese vídeo y se sigue con el resto |
| Descargas simultáneas | Ficheros que se transfieren a la vez. Por defecto 2 |
| Conexiones por fichero | Conexiones paralelas por fichero para acelerar la transferencia (estilo FastTelethon). 1 = método estándar de Telethon. Recomendado 4-8. Por defecto 8 |
| Conversión de vídeo → Encoder | CPU (`libx264`, por defecto), VAAPI (Intel/AMD, recomendado), NVENC (NVIDIA) o QSV (Intel Quick Sync, experimental: necesita el runtime de Intel y no viene en la imagen). Si el encoder hardware falla, se reintenta automáticamente con `libx264`. Ver desplegable de aceleración por hardware |
| Conversión de vídeo → Calidad | Entre las que ofrece el menú (18-35), o la predeterminada de cada encoder (por defecto). Valores más altos reducen calidad y tamaño. `23` es un buen valor inicial |
| Tras descomprimir | Qué hacer con el fichero comprimido después de descomprimirlo desde `/manage`: preguntar (por defecto), borrarlo (con todas sus partes, si es un RAR multiparte) o conservarlo |
| Carpetas propias | Solo lectura: dónde va cada tipo de fichero. Ver desplegable de carpetas |
| Estadísticas anónimas | Una vez al día envía cifras anónimas de uso. Por defecto activado. Ver desplegable de estadísticas |

</details>

<details>
<summary>📁 Cada tipo de fichero en su carpeta</summary>

Todo va a `/downloads` salvo que montes la carpeta de un tipo: entonces va ahí. No hay que activar nada más.

| CARPETA | QUÉ VA AHÍ |
|:------------- | :-------------|
| `/video` | Vídeos |
| `/audio` | Audios |
| `/photo` | Imágenes |
| `/torrent` | Ficheros `.torrent` |
| `/ebook` | Libros electrónicos |
| `/url_video` | Vídeos descargados desde enlaces. Si no la montas, van donde el resto de vídeos |
| `/url_audio` | Audios descargados desde enlaces. Si no la montas, van donde el resto de audios |

```yaml
    volumes:
      - /ruta/para/la/configuracion:/config
      - /ruta/para/descargar/general:/downloads
      - /ruta/para/descargar/video:/video
      - /ruta/para/descargar/audio:/audio
```

En `/settings` → **📁 Carpetas propias** ves a dónde está yendo cada tipo, y en `/list` y `/manage` cada tipo con carpeta propia sale como una categoría aparte.

</details>

<details>
<summary>🎞️ Conversión de vídeo y aceleración por hardware</summary>

Solo se convierte lo que Telegram no puede reproducir. Un MP4 con vídeo H.264 (8 bits) o HEVC (8/10 bits, lo que graban los móviles) y audio AAC/MP3 se envía tal cual; si el vídeo ya vale pero el contenedor (MKV, MOV...), el audio (Opus...) o la etiqueta HEVC (`hev1` en vez de `hvc1`, que necesitan los dispositivos Apple) no, se copia la pista de vídeo sin recodificar y la operación tarda segundos. Solo VP9, AV1 y similares pasan por la conversión completa, que por CPU da siempre H.264 4:2:0 de 8 bits, lo único que Telegram reproduce en todos sus clientes.

Lo que descargas de enlaces se pide por defecto en H.264 y AAC (ver *Preferir compatible con Telegram* en los ajustes), así que normalmente ni siquiera hace falta convertirlo.

La conversión completa va por CPU salvo que elijas otro encoder en `/settings` → **🎞️ Conversión de vídeo**. Para usar la GPU, dale al contenedor acceso a ella:

```yaml
    # Para VAAPI/QSV, da acceso al dispositivo `/dev/dri`:
    devices:
      - /dev/dri:/dev/dri
    # Para NVENC necesitas NVIDIA Container Toolkit y dar acceso a la GPU:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Puedes comprobar qué encoder se está usando en los logs (`[CONVERSION] Encoder mode: ...`). Si el encoder por hardware falla con un vídeo, se reintenta automáticamente por CPU.

</details>

<details>
<summary>🍪 Cookies opcionales para yt-dlp</summary>

Para sitios que requieren una sesión autenticada, crea `cookies/cookies.txt` y
monta la carpeta en `/app/cookies`. El volumen debe ser escribible porque
yt-dlp puede actualizar el cookie jar. Si el fichero no existe, DropBot
continúa usando yt-dlp sin cookies.

```yaml
    volumes:
      - /ruta/para/cookies:/app/cookies
```

</details>

<details>
<summary>🔄 ¿Vienes de la 3.x? No pierdes nada</summary>

- En el primer arranque el bot importa a `settings.json` los valores de tus variables (`LANGUAGE`, `PARALLEL_DOWNLOADS`, `FAST_CONNECTIONS`, `AUTO_DOWNLOAD_FORMAT`, `AUTO_SEND`, `FFMPEG_HW` y `FFMPEG_QUALITY`) y los conserva tal cual. A partir de ahí manda `/settings`, y esas variables ya no se leen: el log te avisa de las que puedes borrar del docker-compose.
- **Añade el volumen `/config`.** Sin él la importación se repite en cada recreación del contenedor y lo que cambies en `/settings` no se guarda.
- **Los `FILTER_*` ya no hacen falta.** Antes había que montar la carpeta *y además* activar su variable; ahora basta con montarla. Si tenías `FILTER_VIDEO=1` y `/video` montada, todo sigue igual. Si tenías un `FILTER_*=1` sin montar su carpeta, esos ficheros se guardaban dentro del contenedor y se perdían al actualizar: ahora van a `/downloads`.
- Un valor no válido en una de esas variables ya no impide arrancar: se ignora con un aviso en el log y se usa el valor por defecto, que puedes corregir desde `/settings`.
- Lo que descargas de enlaces ahora se pide por defecto en H.264 y AAC, que Telegram reproduce sin convertir. Se envía mucho antes, pero un vídeo 4K de YouTube llega en 1080p. Si prefieres lo de antes, desactiva *Preferir compatible con Telegram* en `/settings` → *Enlaces* → *Calidad*.
- Los enlaces a páginas web (`.html`) ya no se descargan como si fueran un fichero: pasan por yt-dlp como cualquier otro enlace.
- `tty: true` no hace falta. Puedes quitarlo, o dejarlo: no molesta.

</details>

<details>
<summary>📊 Estadísticas anónimas</summary>

Desde la 4.0.0 el bot envía una vez al día unas cifras anónimas para saber cuánta gente lo usa y qué funciones se usan más. Así sé dónde poner el esfuerzo. Las estadísticas son públicas: [stats.dgongut.com/dropbot](https://stats.dgongut.com/dropbot).

**Qué se envía:** qué ajustes están activados, qué tipos de fichero tienen carpeta propia (sí o no, nunca la ruta), si hay cookies o GPU disponibles, la versión del bot, la arquitectura, y cuántas veces al día se ha usado cada comando, cada botón y cada tipo de descarga y de conversión. La lista completa, campo a campo, está en [la página de privacidad](https://stats.dgongut.com/dropbot/privacy). El servidor descarta cualquier dato que no esté en ella.

**Qué no se envía nunca:** nombres de ficheros, enlaces, rutas, IDs de Telegram ni nada de lo que escribes. Tu IP no se guarda.

**Cómo se desactiva:** desde `/settings` → *Estadísticas anónimas*, o con `TELEMETRY=false`. Al desactivarlas se borra también el identificador de la instalación. Con *👀 Ver qué se envía* ves exactamente lo que llevaría el próximo envío.

Están activadas por defecto. Nada se envía hasta que el bot lleva al menos 10 minutos en marcha. Tampoco se envía si `/config` no está en un volumen, porque sin él cada vez que se recreara el contenedor contaría como una instalación nueva.

</details>

---

## Solo para desarrolladores - Ejecución con código local

Para su ejecución en local y probar nuevos cambios de código, se necesita renombrar el fichero `.env_example` a `.env` con los valores necesarios para su ejecución. Los ajustes de prueba se guardan en `./config`, que git ignora.
Es necesario establecer un `TELEGRAM_TOKEN` y un `TELEGRAM_ADMIN` correctos y diferentes al de la ejecución normal.

La estructura de carpetas debe quedar:

```
dropbot/
    ├── .env                       # copia de .env_example con tus valores
    ├── .env_example
    ├── .dockerignore
    ├── .gitignore
    ├── .github/workflows/ci.yml   # tests, pyflakes y, en cada tag, la imagen
    ├── LICENSE
    ├── README.md
    ├── README_EN.md
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── Dockerfile
    ├── Dockerfile_local
    ├── docker-compose.yaml
    ├── yt-dlp.conf
    ├── dropbot.py
    ├── config.py                  # variables de entorno y constantes
    ├── settings.py                # ajustes de /settings, validados
    ├── store.py                   # settings.json en /config
    ├── migration.py               # importa las variables de la 3.x
    ├── stats.py                   # qué se cuenta para las estadísticas
    ├── state.py
    ├── basic.py
    ├── logger.py
    ├── translations.py
    ├── message_queue.py
    ├── handlers
    │   ├── manage.py
    │   └── settings.py            # pantallas de /settings
    ├── services
    │   ├── donors_service.py
    │   ├── extraction_service.py
    │   └── video_service.py
    ├── utils
    │   ├── fast_telethon.py
    │   ├── file_helpers.py
    │   ├── limiter.py
    │   ├── mounts.py
    │   └── telegram_helpers.py
    ├── tests                      # pytest; ver más abajo
    └── locale
        ├── en.json
        └── es.json
```

Para levantarlo habría que ejecutar en esa ruta: `docker compose up -d`

Para detenerlo y eliminarlo, junto con la imagen que construye: `docker compose down --rmi local`

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Los tests no necesitan red ni un token real: sustituyen el cliente de Telegram
y redirigen las carpetas de descarga y `/config` a un directorio temporal, y
yt-dlp y wget por scripts que imitan su salida. Se ejecutan automáticamente en
cada push a `main` y en cada pull request, junto con pyflakes; la imagen se construye en cada
tag.

Las conversiones, las miniaturas y los RAR se prueban con `ffmpeg` y `unrar` de
verdad; si no están instalados, esos tests se saltan. Para pasarlos todos sin
instalar nada, dentro de la imagen de desarrollo (`docker compose build`):

```bash
docker run --rm --entrypoint sh -v "$PWD":/src:ro -w /src dropbot-dropbot -c "pip install -q --break-system-packages -r requirements-dev.txt && python3 -m pytest -q -p no:cacheprovider"
```

Con `--cov=. --cov-report=term-missing` se ve qué queda sin cubrir.
