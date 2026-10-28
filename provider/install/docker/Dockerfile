# HAMA provider image. Build context = the provider/ folder:
#   docker build -f install/docker/Dockerfile -t hama-provider:local .        (from provider/)
# Release images (multi-arch, ghcr.io) are built by .github/workflows/release.yml.
FROM python:3.12-alpine
ARG VERSION=dev
LABEL org.opencontainers.image.title="HAMA provider" \
      org.opencontainers.image.description="HAMA – AniDB metadata provider for Plex (custom metadata provider, PMS 1.43+)" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}"
WORKDIR /app
COPY hama_provider ./hama_provider
COPY run.py config.example.json ./
ENV HAMA_HOST=0.0.0.0 \
    HAMA_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir /data && chown 1000:1000 /data
VOLUME /data
EXPOSE 32550
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('HAMA_PORT', '32550'), timeout=8)" || exit 1
CMD ["python", "-m", "hama_provider"]
