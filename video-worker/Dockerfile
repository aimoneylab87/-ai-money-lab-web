FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY provider_client.py .
COPY storage.py .
COPY worker.py .

CMD ["python", "worker.py"]
