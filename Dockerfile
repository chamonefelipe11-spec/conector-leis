FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mcp>=1.20,<2"

WORKDIR /app
COPY app/ /app/

ENV REPOS_DIR=/dados/repos \
    DB_PATH=/dados/indice.sqlite \
    PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["python", "servidor.py"]
