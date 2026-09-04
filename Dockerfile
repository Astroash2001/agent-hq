FROM python:3.10-slim

# Create a non-root system user and home directory
RUN groupadd -r agenthq && useradd -r -g agenthq -m -d /home/agenthq agenthq

WORKDIR /app

# Install runtime dependencies only (development tools stay in requirements-dev.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source directories
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Ensure runtime mission data directory exists and is writable by non-root user
RUN mkdir -p backend/data && chown -R agenthq:agenthq /app

USER agenthq

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

CMD ["python", "backend/main.py"]
