FROM python:3.12-slim

LABEL org.opencontainers.image.title="vgpu-driver-operator"

# Create non-root user
RUN groupadd --gid 65532 app && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /sbin/nologin app

WORKDIR /app

# Copy and install dependencies first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source package
COPY src/ ./src/

USER 65532

ENTRYPOINT ["python", "-m", "vgpu_driver_operator"]
CMD ["controller"]
