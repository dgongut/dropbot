# Dockerfile optimizado para producción
FROM ubuntu:26.04

# Build arguments
ARG VERSION=4.0.1
ARG POT_PROVIDER_VERSION=2.0.2
# El commit de ese tag, que es lo que se descarga: un tag se puede mover a otro
# código y un commit no. Al subir de versión hay que cambiar los dos
ARG POT_PROVIDER_COMMIT=26475e9d3665b972c4aeb83b4dbaafcf64b88438
# Deno con versión fija: el zip de su release de GitHub se comprueba con el
# sha256 que Deno publica junto a él, uno por arquitectura, contrastado con el
# que publica en dl.deno.land. Al subir de versión, elegir una en la que los dos
# coincidan: si solo se mira GitHub, quien suba el zip pone también el hash
ARG DENO_VERSION=2.9.7
ARG DENO_SHA256_AMD64=c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490
ARG DENO_SHA256_ARM64=c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf

# Metadata (etiquetas OCI estándar: las leen herramientas como docker-controller-bot)
LABEL org.opencontainers.image.title="dropbot" \
      org.opencontainers.image.description="DropBot - Telegram file management bot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.authors="dgongut" \
      org.opencontainers.image.source="https://github.com/dgongut/dropbot" \
      org.opencontainers.image.url="https://hub.docker.com/r/dgongut/dropbot" \
      org.opencontainers.image.licenses="GPL-3.0"

# Evitar prompts interactivos durante la instalación
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Instalar dependencias del sistema, descargar código e instalar dependencias Python en una sola capa
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
        python3 \
        python3-pip \
        tzdata \
        ffmpeg \
        unrar \
        tini \
        curl \
        unzip && \
    # Driver VA-API de Intel para VAAPI/QSV (paquete solo-amd64: en arm no hay iGPU Intel)
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi && \
    # Instalar Deno (requerido por yt-dlp para descargas de YouTube)
    # Deno es el runtime de JavaScript recomendado para resolver desafíos JS de YouTube
    # Solo hay binarios para amd64 y arm64
    case "$(dpkg --print-architecture)" in \
        amd64) deno_target=x86_64-unknown-linux-gnu deno_sha256=${DENO_SHA256_AMD64} ;; \
        arm64) deno_target=aarch64-unknown-linux-gnu deno_sha256=${DENO_SHA256_ARM64} ;; \
        *) echo "Deno no tiene binario para $(dpkg --print-architecture)" && exit 1 ;; \
    esac && \
    curl -fsSL https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_target}.zip -o /tmp/deno.zip && \
    echo "${deno_sha256}  /tmp/deno.zip" | sha256sum -c - && \
    unzip -q /tmp/deno.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/deno && \
    # Verificar instalación de Deno
    deno --version && \
    # Instalar el proveedor de PO Token (bgutil), necesario para que YouTube no
    # devuelva HTTP 403 en los formatos de alta calidad. Se ejecuta como servidor
    # HTTP local junto al bot, usando el mismo Deno ya instalado
    wget -q https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/${POT_PROVIDER_COMMIT}.zip -O /tmp/pot.zip && \
    unzip -q /tmp/pot.zip -d /tmp && \
    mv /tmp/bgutil-ytdlp-pot-provider-${POT_PROVIDER_COMMIT} /opt/bgutil-ytdlp-pot-provider && \
    cd /opt/bgutil-ytdlp-pot-provider/server && \
    deno install --allow-scripts=npm:canvas --frozen && \
    cd /app && \
    # Descargar y extraer código
    # Se usa el .zip en lugar del .tar.gz porque GNU tar falla al extraer bajo
    # emulación amd64 (los syscalls de creación de ficheros devuelven ENOSYS)
    wget -q https://github.com/dgongut/dropbot/archive/refs/tags/v${VERSION}.zip -O /tmp/dropbot.zip && \
    unzip -q /tmp/dropbot.zip -d /tmp && \
    mv /tmp/dropbot-${VERSION}/* /app/ && \
    # Mover archivo de configuración de yt-dlp para habilitar EJS
    # Esto permite que yt-dlp descargue automáticamente los scripts EJS necesarios para YouTube
    mv /app/yt-dlp.conf /etc/yt-dlp.conf && \
    # Instalar dependencias de Python antes de limpiar pip
    # En Ubuntu 26.04 con Python 3.14, necesitamos --break-system-packages para pip en contenedores
    pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt && \
    # Limpiar archivos temporales y cache (mantener wget y ca-certificates para descargas HTTPS)
    rm -rf /tmp/* && \
    apt-get remove -y python3-pip curl unzip && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Healthcheck: verifica que el bot está vivo y respondiendo (fichero heartbeat actualizado en <60s)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD [ $(($(date +%s) - $(stat -c %Y /tmp/dropbot_heartbeat 2>/dev/null || echo 0))) -lt 60 ] || exit 1

# tini como PID 1: le pasa las señales al bot y recoge los procesos huérfanos,
# como el ffmpeg que deja un yt-dlp cancelado, que si no quedarían como zombis.
# Con -s los sigue recogiendo cuando Docker pone su propio init delante
# (init: true o --init), y así no avisa en cada arranque de que no es el PID 1
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "python3", "dropbot.py"]

# Cuándo y de qué commit, pasados al construir:
#   --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) --build-arg VCS_REF=$(git rev-parse HEAD)
# Al final a propósito: cambian en cada construcción, y todo lo que fuera
# detrás se reconstruiría siempre en vez de salir de la caché.
ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"
