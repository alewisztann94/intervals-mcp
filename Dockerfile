FROM python:3.12-slim

WORKDIR /app

# Dependencies first so code changes don't bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Don't run as root.
RUN useradd --system --no-create-home app
USER app

ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "server.py"]
