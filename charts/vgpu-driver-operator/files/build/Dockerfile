# v0.1.0: amd64 only.

# FLATCAR_IMAGE_REF is the canonical "what to pull" reference.
# - Tag-based (default): repo:version+suffix assembled from the three fallback args below.
# - Digest-pinned: pass --build-arg FLATCAR_IMAGE_REF=ghcr.io/andrewreid/flatcar-developer@sha256:<digest>
#   The operator can construct this value and pass it directly without touching other args.
ARG FLATCAR_IMAGE_REPO=ghcr.io/andrewreid/flatcar-developer
ARG FLATCAR_IMAGE_SUFFIX=-sources
ARG FLATCAR_VERSION
ARG FLATCAR_IMAGE_REF=${FLATCAR_IMAGE_REPO}:${FLATCAR_VERSION}${FLATCAR_IMAGE_SUFFIX}
FROM ${FLATCAR_IMAGE_REF}

SHELL ["/bin/bash", "-c"]

ARG DRIVER_VERSION
ARG FLATCAR_VERSION
ARG DRIVER_TYPE=vgpu
ARG VGPU_LICENSE_SERVER_TYPE=NLS
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
RUN chmod +x /usr/local/bin/nvidia-driver

WORKDIR /drivers

LABEL org.opencontainers.image.title="NVIDIA vGPU driver (Flatcar runtime builder)" \
      org.opencontainers.image.version="${DRIVER_VERSION}" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.source="https://github.com/andrewreid/vgpu-driver-builder" \
      org.opencontainers.image.vendor="andrewreid" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="NVIDIA vGPU driver image that compiles kernel modules at runtime on Flatcar Linux nodes" \
      flatcar.version="${FLATCAR_VERSION}"

ENTRYPOINT ["nvidia-driver"]
