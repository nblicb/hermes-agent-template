FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

# Install hermes-agent as a package (gives us the `hermes` CLI entry point)
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent && \
    cd /tmp/hermes-agent && \
    uv pip install --system --no-cache -e ".[all]" && \
    rm -rf /tmp/hermes-agent/.git

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY rate_limit.py /app/rate_limit.py
COPY commands.py /app/commands.py
COPY ticker_resolver.py /app/ticker_resolver.py
COPY asr.py /app/asr.py
COPY gateway_wrapper.py /app/gateway_wrapper.py
COPY templates/ /app/templates/
COPY skills/ /app/skills/
COPY config/ /app/config/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

CMD ["/app/start.sh"]
