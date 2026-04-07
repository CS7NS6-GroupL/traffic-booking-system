FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY services/booking-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code only — no shared/ dependency (auth is inlined)
COPY services/booking-service/ /app/

ENV SERVICE_NAME=booking-service

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
