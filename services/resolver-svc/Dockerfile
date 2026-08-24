# Multi-stage build per docs/engineering/conventions.md — uv sync in a
# builder stage, copy the resulting venv into a slim runtime stage. No
# shared base image beyond the public python:3.12-slim tag.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY shared/ shared/
COPY services/resolver-svc/ services/resolver-svc/
RUN uv sync --frozen --package resolver-svc --no-dev --no-editable

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /workspace/.venv /app/.venv
COPY --from=builder /workspace/services/resolver-svc/src /app/src
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
EXPOSE 8080
# python -m uvicorn, not the uvicorn script directly — its shebang hardcodes
# the builder stage's venv path, which doesn't exist in this runtime image.
CMD ["python", "-m", "uvicorn", "resolver_svc.main:app", "--host", "0.0.0.0", "--port", "8080"]
