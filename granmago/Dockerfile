ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.11-alpine3.18
FROM $BUILD_FROM

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every local module server.py imports has to be named here.
# tests/test_image_copy.py fails if one is missing.
COPY server.py granmago_strategy.py ./
COPY static/ static/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

RUN mkdir -p /data

EXPOSE 8787

CMD [ "/run.sh" ]
