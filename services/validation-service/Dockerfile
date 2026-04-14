FROM python:3.11-slim

WORKDIR /app

# Install service dependencies
COPY services/validation-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared auth dependencies
COPY shared/requirements.txt /tmp/shared-req.txt
RUN pip install --no-cache-dir -r /tmp/shared-req.txt

# Copy shared library
COPY shared/ /app/shared/

# Copy data service HTTP client (exposes same interface as data_service.py)
COPY services/data/data_client.py /app/data/data_service.py

# Copy Redis rebuild script so the watchdog can trigger a full rebuild
COPY services/data/rebuild_redis.py /app/rebuild_redis.py

# Copy service code
COPY services/validation-service/ /app/

ENV SERVICE_NAME=validation-service
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
