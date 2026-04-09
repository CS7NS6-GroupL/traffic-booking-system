FROM python:3.11-slim

WORKDIR /app

COPY services/data/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/data/data_service.py .
COPY services/data/main.py .

ENV PYTHONUNBUFFERED=1
ENV SERVICE_NAME=data-service

EXPOSE 8009
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8009"]
