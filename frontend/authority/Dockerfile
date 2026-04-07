FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

ENV DNS_SERVER=geodns
ENV DNS_PORT=53
ENV DNS_NAME=api.demo.local
ENV NGINX_PORT=80
ENV REQUEST_TIMEOUT=5

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]