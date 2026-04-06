FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY geoDNS.py .

EXPOSE 53/udp

CMD ["python", "geoDNS.py"]

#docker build -t geodns-demo .
#docker run -it --rm -p 1053:53/udp geodns-demo
