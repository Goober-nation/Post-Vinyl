FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.12-slim AS runtime

RUN useradd --uid 1000 --create-home postvinyl
WORKDIR /app

COPY --from=builder /root/.local /home/postvinyl/.local
COPY app/ ./app/

# Post-Vinyl's own folders, named so they don't collide with other apps' (#118)
RUN mkdir -p /postvinyl/data /postvinyl/config \
    && chown -R postvinyl:postvinyl /app /postvinyl /home/postvinyl/.local

USER postvinyl
ENV PATH=/home/postvinyl/.local/bin:$PATH \
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

CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
