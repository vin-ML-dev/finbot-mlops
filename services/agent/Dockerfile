FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY configs/ ./configs/

RUN useradd -u 10002 -m appuser
USER appuser

EXPOSE 9108
# the agent runs a continuous loop; metrics (incl. heartbeat) served on 9108
CMD ["python", "-m", "app.main"]
