FROM alpine:3.21.3

ARG VERSION=1.5.0

WORKDIR /app
RUN wget https://github.com/dgongut/dropbot/archive/refs/tags/v${VERSION}.tar.gz -P /tmp
RUN tar -xf /tmp/v${VERSION}.tar.gz
RUN mv dropbot-${VERSION}/dropbot.py /app
RUN mv dropbot-${VERSION}/config.py /app
RUN mv dropbot-${VERSION}/translations.py /app
RUN mv dropbot-${VERSION}/basic.py /app
RUN mv dropbot-${VERSION}/debug.py /app
RUN mv dropbot-${VERSION}/locale /app
RUN mv dropbot-${VERSION}/requirements.txt /app
RUN rm /tmp/v${VERSION}.tar.gz
RUN rm -rf dropbot-${VERSION}/
RUN apk add --no-cache python3 py3-pip tzdata ffmpeg
RUN export PIP_BREAK_SYSTEM_PACKAGES=1; pip3 install --no-cache-dir -Ur /app/requirements.txt

ENTRYPOINT ["python3", "dropbot.py"]