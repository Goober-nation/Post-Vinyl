FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.12-slim AS runtime

RUN useradd --uid 1000 --create-home postvinyl
WORKDIR /app

COPY --from=builder /root/.local /home/postvinyl/.local
COPY app/ ./app/

RUN mkdir -p /app/data && chown -R postvinyl:postvinyl /app /home/postvinyl/.local

USER postvinyl
# beets ships a `beet` CLI into ~/.local/bin — app/services/beets.py invokes
# it as a subprocess, so it has to be on PATH, not just importable.
ENV PATH=/home/postvinyl/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Liveness, not readiness: /api/system/ping answers from the event loop and
# talks to nothing. This used to probe /api/system/status, which live-checks
# slskd and Navidrome and can legitimately take ~15s — three times this
# timeout — so a slow slskd was enough to mark postvinyl unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/system/ping', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
