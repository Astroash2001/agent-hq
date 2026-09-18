FROM python:3.10-slim

# Create the runtime user. UID 1000 on purpose: Hugging Face Spaces runs a
# Docker container as that id, and a process that cannot write its own mission
# directory fails at the first artifact rather than at startup.
RUN groupadd -g 1000 agenthq && useradd -u 1000 -g agenthq -m -d /home/agenthq agenthq

WORKDIR /app

# Install runtime dependencies only (development tools stay in requirements-dev.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The browser, for `render_page`. Kept out of requirements.txt because it is
# optional at the code level -- `tool_registry` registers the tool only where a
# browser is actually installed, so a host without one simply does not offer it.
#
# Off by default, because the image's usual home cannot afford it: Chromium
# peaked at 829MB resident in testing and Render's free instance has 512MB. A
# browser-enabled image there registers `render_page`, an agent calls it, and
# the mission dies of OOM. Build with --build-arg BROWSER=1 on a host with the
# memory for it.
ARG BROWSER=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$BROWSER" = "1" ]; then \
        pip install --no-cache-dir playwright==1.63.0 && \
        playwright install --with-deps chromium && \
        chmod -R a+rX /opt/ms-playwright; \
    fi

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
