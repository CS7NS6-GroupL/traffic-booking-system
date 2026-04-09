FROM python:3.11-slim

WORKDIR /app

# Install service dependencies
COPY services/booking-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install shared auth dependencies
COPY shared/requirements.txt /tmp/shared-req.txt
RUN pip install --no-cache-dir -r /tmp/shared-req.txt

# Copy shared library
COPY shared/ /app/shared/

# Copy service code
COPY services/booking-service/ /app/

ENV PYTHONUNBUFFERED=1
ENV SERVICE_NAME=booking-service

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
