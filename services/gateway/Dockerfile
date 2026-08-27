FROM python:3.12-slim

WORKDIR /app

# install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code
COPY app/ ./app/

# non-root user
RUN useradd -u 10001 -m appuser
USER appuser

EXPOSE 8000
# uvicorn serving the FastAPI app; 2 workers is plenty for a gateway pod (HPA scales pods)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
