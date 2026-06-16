FROM v2fly/v2fly-core:v5.15.1

RUN apk add --no-cache \
    bash \
    dcron \
    procps \
    py3-pip \
    python3 \
    sqlite \
    supervisor \
    tzdata

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && ln -sf /opt/venv/bin/python /usr/bin/python

COPY app /app/app
COPY config.yaml /app/config.yaml
COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV APP_CONFIG_PATH=/app/config.yaml \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN mkdir -p /data /etc/v2ray \
    && chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080 10086

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
