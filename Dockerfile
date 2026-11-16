# --- Stage 1: build native wheels -------------------------------------------
# TgCrypto publishes no prebuilt wheel for CPython 3.12, so it has to be compiled.
# The compiler lives only in this throwaway stage and never reaches the final image.
FROM python:3.12-slim AS wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --wheel-dir /wheels "tgcrypto>=1.2"

# --- Stage 2: runtime image (no compiler) -------------------------------------
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# Install the prebuilt TgCrypto wheel without leaving it in an image layer.
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    pip install --no-cache-dir /wheels/*.whl
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"
RUN mkdir -p /app/data
CMD ["python", "-m", "app.main"]
