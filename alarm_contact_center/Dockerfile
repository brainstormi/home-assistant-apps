FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/alarm_contact_center

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Unhealthy when the panel port stops accepting connections; with the App's
# watchdog switch on, the Supervisor restarts it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8099), 3).close()"]

CMD ["python", "-m", "app.server"]
