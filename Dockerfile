# Build stage: resolve and install locked dependencies, then the project itself
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependencies first so this layer is reused when only source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: plain Python, no uv, no build context
FROM python:3.13-slim-bookworm

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 eosdk \
    && useradd --uid 1000 --gid eosdk --create-home eosdk

COPY --from=builder /opt/venv /opt/venv

USER eosdk
WORKDIR /home/eosdk

ENTRYPOINT ["eo"]
CMD ["--help"]
