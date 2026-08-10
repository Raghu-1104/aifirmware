# syntax=docker/dockerfile:1
#
# Multi-stage build: a wheel is built once, then installed into a slim runtime
# image that carries no build toolchain and runs as an unprivileged user.

FROM python:3.12-slim AS builder

WORKDIR /src
RUN pip install --no-cache-dir build

COPY pyproject.toml README.md ./
COPY fwcopilot ./fwcopilot
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="fwcopilot" \
      org.opencontainers.image.description="Firmware engineering assistant with board and datasheet context" \
      org.opencontainers.image.source="https://github.com/Raghu-1104/aifirmware" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FWCOPILOT_LOG_FORMAT=json

# git is genuinely useful in the workspace (the assistant reads repo state);
# everything else stays out of the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 fwcopilot

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl[server] && rm -f /tmp/*.whl

USER fwcopilot
WORKDIR /workspace

EXPOSE 8765

# The container is useless without a project mounted at /workspace, so the
# default command serves whatever is mounted there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["fwcopilot"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
