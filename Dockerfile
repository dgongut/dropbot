# Dockerfile optimizado para producción
FROM ubuntu:22.04

# Build argument
ARG VERSION=3.1.6

# Metadata
LABEL maintainer="dgongut"
LABEL description="DropBot - Telegram file management bot"
LABEL version="${VERSION}"

# Evitar prompts interactivos durante la instalación
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Instalar dependencias del sistema, descargar código e instalar dependencias Python en una sola capa
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
        python3 \
        python3-pip \
        tzdata \
        ffmpeg \
        unrar \
        curl \
        unzip && \
    # Instalar Deno (requerido por yt-dlp para descargas de YouTube)
    # Deno es el runtime de JavaScript recomendado para resolver desafíos JS de YouTube
    curl -fsSL https://deno.land/install.sh | sh && \
    mv /root/.deno/bin/deno /usr/local/bin/deno && \
    chmod +x /usr/local/bin/deno && \
    # Verificar instalación de Deno
    deno --version && \
    # Descargar y extraer código
    wget -q https://github.com/dgongut/dropbot/archive/refs/tags/v${VERSION}.tar.gz -O /tmp/dropbot.tar.gz && \
    tar -xzf /tmp/dropbot.tar.gz -C /tmp && \
    mv /tmp/dropbot-${VERSION}/* /app/ && \
    # Mover archivo de configuración de yt-dlp para habilitar EJS
    # Esto permite que yt-dlp descargue automáticamente los scripts EJS necesarios para YouTube
    mv /app/yt-dlp.conf /etc/yt-dlp.conf && \
    # Instalar dependencias de Python antes de limpiar pip
    pip3 install --no-cache-dir -r /app/requirements.txt && \
    # Limpiar archivos temporales y cache (mantener wget y ca-certificates para descargas HTTPS)
    rm -rf /tmp/* /root/.deno && \
    apt-get remove -y python3-pip curl unzip && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Healthcheck (verificar que el proceso Python esté corriendo)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD pgrep -f "python3 dropbot.py" || exit 1

ENTRYPOINT ["python3", "dropbot.py"]