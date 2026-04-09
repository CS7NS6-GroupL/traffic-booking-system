FROM python:3.11-slim

WORKDIR /app

# Install service dependencies
COPY services/journey-management/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared auth dependencies
COPY shared/requirements.txt /tmp/shared-req.txt
RUN pip install --no-cache-dir -r /tmp/shared-req.txt

# Copy shared library
COPY shared/ /app/shared/

# Copy data service HTTP client (exposes same interface as data_service.py)
COPY services/data/data_client.py /app/data/data_service.py

# Copy service code
COPY services/journey-management/ /app/

ENV PYTHONUNBUFFERED=1
ENV SERVICE_NAME=journey-management

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
