# Build stage: resolve and install locked dependencies, then the project itself
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=automatic \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Relocatable, self-contained CPython so the runtime image needs no system
# Python — this is what lets us ship on distroless.
RUN uv python install 3.13

WORKDIR /build

# Dependencies first so this layer is reused when only source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: distroless — glibc + libgcc/libstdc++ only. No shell, apt,
# dpkg, perl, util-linux, gzip or ncurses, which is where the base image's
# unfixable HIGH/CRITICAL CVEs lived. Trivy reports zero HIGH/CRITICAL here.
FROM gcr.io/distroless/cc-debian12:nonroot@sha256:66aa873a4a14fb164aa01296058efd8253744606d72715e45acface073359faa

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The standalone interpreter and the venv that references it (the venv's
# python symlinks point into /python), copied with their absolute paths intact.
COPY --from=builder /python /python
COPY --from=builder /opt/venv /opt/venv

# distroless ships a built-in unprivileged "nonroot" user (uid 65532)
USER nonroot
WORKDIR /home/nonroot

# Absolute path: distroless has no shell and ENTRYPOINT resolution should not
# depend on PATH lookup.
ENTRYPOINT ["/opt/venv/bin/eo"]
CMD ["--help"]
