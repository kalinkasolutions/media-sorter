FROM python:3.13-slim

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY media_sorter.py /app/media_sorter.py

ARG VERSION=dev
ENV VERSION=$VERSION \
    PYTHONUNBUFFERED=1 \
    DOWNLOADS=/downloads \
    MOVIES=/movies \
    SERIES=/series

CMD ["python", "/app/media_sorter.py"]
