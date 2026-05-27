# v0.1.0: amd64 only.

# FLATCAR_IMAGE_REF is the canonical "what to pull" reference.
# - Tag-based (default): repo:version+suffix assembled from the three fallback args below.
# - Digest-pinned: pass --build-arg FLATCAR_IMAGE_REF=ghcr.io/andrewreid/flatcar-developer@sha256:<digest>
#   The operator can construct this value and pass it directly without touching other args.
ARG FLATCAR_IMAGE_REPO=ghcr.io/andrewreid/flatcar-developer
ARG FLATCAR_IMAGE_SUFFIX=-sources
ARG FLATCAR_VERSION
ARG FLATCAR_IMAGE_REF=${FLATCAR_IMAGE_REPO}:${FLATCAR_VERSION}${FLATCAR_IMAGE_SUFFIX}

FROM ubuntu:22.04 AS nvlts-downloader

SHELL ["/bin/bash", "-c"]

ARG NVLTS_VERSION=1.0.3
ARG NVLTS_SHA256=98de378b667d1054607771e4a68dd038202807685b935c3fc8d4d6e8b1aff15d

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -euo pipefail \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
         ca-certificates \
         curl \
         tar \
    && curl -fsSL -o /tmp/nvlts.tar.gz \
         "https://git.collinwebdesigns.de/vgpu/nvlts/-/releases/v${NVLTS_VERSION}/downloads/nvlts_${NVLTS_VERSION}_linux_amd64.tar.gz" \
    && printf '%s  /tmp/nvlts.tar.gz\n' "${NVLTS_SHA256}" | sha256sum -c - \
    && mkdir -p /opt/nvlts \
    && tar -xzf /tmp/nvlts.tar.gz -C /opt/nvlts nvlts LICENSE \
    && chmod +x /opt/nvlts/nvlts

FROM ${FLATCAR_IMAGE_REF}

SHELL ["/bin/bash", "-c"]

ARG DRIVER_VERSION
ARG FLATCAR_VERSION
ARG DRIVER_TYPE=vgpu
ARG VGPU_LICENSE_SERVER_TYPE=NLS
ARG NVLTS_VERSION=1.0.3
ARG GIT_REVISION=""
ARG BUILD_CREATED=""

ENV DRIVER_VERSION=${DRIVER_VERSION} \
    NVIDIA_VISIBLE_DEVICES=void \
    DRIVER_TYPE=${DRIVER_TYPE} \
    VGPU_LICENSE_SERVER_TYPE=${VGPU_LICENSE_SERVER_TYPE}

# Staging directory for runtime operations
RUN mkdir -p /drivers

# Copy the installer from the build context into /drivers
# NOTE: The build job must place the .run here (init container already does this)
COPY NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run /drivers/

# NVIDIA driver init script for Flatcar (runtime compilation on node)
COPY nvidia-driver /usr/local/bin/nvidia-driver
RUN mkdir -p /usr/share/doc/nvlts
COPY --from=nvlts-downloader /opt/nvlts/nvlts /usr/local/bin/nvlts
COPY --from=nvlts-downloader /opt/nvlts/LICENSE /usr/share/doc/nvlts/LICENSE
RUN chmod +x /usr/local/bin/nvidia-driver

WORKDIR /drivers

LABEL org.opencontainers.image.title="NVIDIA vGPU driver (Flatcar runtime builder)" \
      org.opencontainers.image.version="${DRIVER_VERSION}" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.source="https://github.com/andrewreid/vgpu-driver-builder" \
      org.opencontainers.image.vendor="andrewreid" \
      org.opencontainers.image.licenses="Apache-2.0 AND AGPL-3.0-only" \
      org.opencontainers.image.nvlts.source="https://git.collinwebdesigns.de/vgpu/nvlts" \
      org.opencontainers.image.nvlts.version="${NVLTS_VERSION}" \
      org.opencontainers.image.description="NVIDIA vGPU driver image that compiles kernel modules at runtime on Flatcar Linux nodes" \
      flatcar.version="${FLATCAR_VERSION}"

ENTRYPOINT ["nvidia-driver"]
