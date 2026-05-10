FROM python:3.12-slim

LABEL org.opencontainers.image.title="vgpu-driver-operator"

ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN groupadd --gid 65532 app && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /sbin/nologin app

WORKDIR /app

# Copy source and install
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

USER 65532

ENTRYPOINT ["python", "-m", "vgpu_driver_operator"]
CMD ["controller"]
