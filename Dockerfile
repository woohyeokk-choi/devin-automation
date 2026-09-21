FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORTAL_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY clients ./clients
COPY portal ./portal
COPY scripts ./scripts
COPY scenarios ./scenarios

RUN useradd --uid 10001 --create-home portal && mkdir -p /data && chown portal /data
USER portal
VOLUME ["/data"]
EXPOSE 8090

CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "8090"]
