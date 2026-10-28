# HAMA Plex metadata provider. Build from the provider/ folder:
#   docker build -f install/docker/Dockerfile -t hama-provider .
FROM python:3.12-alpine
WORKDIR /app
COPY hama_provider ./hama_provider
COPY run.py config.example.json ./
ENV HAMA_HOST=0.0.0.0 \
    HAMA_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir /data && chown 1000:1000 /data
VOLUME /data
EXPOSE 32550
USER 1000:1000
CMD ["python", "-m", "hama_provider"]
