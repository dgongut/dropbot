FROM alpine:3.18.6

ENV TELEGRAM_TOKEN=abc
ENV TELEGRAM_ADMIN=abc
ENV TELEGRAM_API_HASH=abc
ENV TELEGRAM_API_ID=abc
ENV LANGUAGE=ES
ENV DEFAULT_DOWNLOAD_PATH=abc
ENV DEFAULT_DOWNLOAD_AUDIO=abc
ENV DEFAULT_DOWNLOAD_VIDEO=abc
ENV DEFAULT_DOWNLOAD_PHOTO=abc
ENV DEFAULT_DOWNLOAD_DOCUMENT=abc
ENV DEFAULT_DOWNLOAD_TORRENT=abc
ARG VERSION=0.9.0

WORKDIR /app
RUN wget https://github.com/dgongut/dropbot/archive/refs/tags/v${VERSION}.tar.gz -P /tmp
RUN tar -xf /tmp/v${VERSION}.tar.gz
RUN mv dropbot-${VERSION}/* /app
RUN rm /tmp/v${VERSION}.tar.gz
RUN rm -rf dropbot-${VERSION}/
RUN apk add --no-cache python3 py3-pip
RUN pip3 install pyrogram==2.0.106 TgCrypto-pyrofork==1.2.7

ENTRYPOINT ["python3", "dropbot.py"]