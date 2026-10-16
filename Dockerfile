FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

RUN apt-get update && apt-get install -y \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY daily_triage.py .
COPY captcha_ocr.py .
COPY start.sh .
RUN chmod +x start.sh

RUN mkdir -p /data/playlists

CMD ["/app/start.sh"]
