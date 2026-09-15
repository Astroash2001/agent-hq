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

# The scouting test inputs. Seeded, so this CSV is identical to the one a local
# run queries. --no-truth: the image carries the inputs and never the answers,
# which only the checker (evals/mission_check.py, run from outside) reads.
RUN python backend/fixtures/make_scouting_fixtures.py --no-truth

# Ensure runtime mission data directory exists and is writable by non-root user.
# backend/data holds runtime state only, so a persistent disk mounted over it
# hides nothing the image needs.
RUN mkdir -p backend/data && chown -R agenthq:agenthq /app

USER agenthq

ENV ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT', '8080'))" || exit 1

CMD ["python", "backend/main.py"]
