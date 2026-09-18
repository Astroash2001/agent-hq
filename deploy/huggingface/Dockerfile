# The Hugging Face Space build.
#
# The Space repository holds only this file and its README. The code comes
# from GitHub at build time, for two reasons:
#
#   * Hugging Face refuses a git push containing binary files unless they go
#     through its LFS/Xet storage, and this project tracks 35 images. Cloning
#     sidesteps that entirely.
#   * Nothing on the developer's disk is ever uploaded, so a local
#     backend/.env cannot end up in a public Space by accident. Secrets are set
#     in the Space's settings and arrive as environment variables.
#
# To ship a new version: push to GitHub, then "Factory rebuild" the Space.
# A plain restart reuses the cached clone.
FROM python:3.10-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/Astroash2001/agent-hq.git /app
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt

# The browser for `render_page`. A Space's free CPU tier has 16GB; Chromium
# peaked at 829MB in testing, which is why this host can offer the tool and
# the 512MB one could not.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir playwright==1.63.0 \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright

RUN python backend/fixtures/make_scouting_fixtures.py --no-truth

# A Space runs its container as uid 1000; the app must own its data directory.
RUN useradd -u 1000 -m agenthq \
 && mkdir -p backend/data \
 && chown -R agenthq /app
USER agenthq

ENV ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=7860
EXPOSE 7860

CMD ["python", "backend/main.py"]
