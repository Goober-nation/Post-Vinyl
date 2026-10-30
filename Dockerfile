FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app/ ./app/
COPY post-vinyl.example.yaml entrypoint.sh ./

# Starts as root only to create Post-Vinyl's folders (named so they don't
# collide with other apps', #118) and hand them to PUID:PGID; the app itself
# runs as that user (entrypoint.sh).
ENV PUID=1000 \
    PGID=1000 \
    PYTHONUNBUFFERED=1 \
    PV_DB_PATH=/postvinyl/data/post-vinyl.db \
    PV_CONFIG_PATH=/postvinyl/config/post-vinyl.yaml

EXPOSE 8000

# Liveness, not readiness: /api/system/ping answers from the event loop and
# talks to nothing. This used to probe /api/system/status, which live-checks
# slskd and Navidrome and can legitimately take ~15s — three times this
# timeout — so a slow slskd was enough to mark postvinyl unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/system/ping', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
