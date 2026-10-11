# gated-relay: a closed Nostr relay (NIP-42 sign-in + allowlist + scoped reads).
FROM python:3.12-slim
WORKDIR /usr/src/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gated_relay.py .
RUN useradd --system --uid 10001 relay && mkdir -p db && chown relay db
USER relay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)" || exit 1
CMD ["python", "-I", "gated_relay.py"]
