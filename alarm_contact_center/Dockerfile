FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/alarm_contact_center

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.server"]
