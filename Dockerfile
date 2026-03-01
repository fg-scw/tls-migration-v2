FROM ubuntu:24.04

LABEL maintainer="vmware2scw"
LABEL description="VMware to Scaleway migration tool"

ENV DEBIAN_FRONTEND=noninteractive
ENV LIBGUESTFS_BACKEND=direct

# System dependencies for disk conversion and guest OS manipulation
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    qemu-utils \
    libguestfs-tools \
    guestfs-tools \
    nbdkit \
    linux-image-generic \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Optional: Download VirtIO drivers for Windows guests
# Uncomment if you need Windows VM migration support
# RUN mkdir -p /opt/virtio-win && \
#     curl -L -o /opt/virtio-win/virtio-win.iso \
#     https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
COPY vmware2scw/ vmware2scw/
RUN pip install --break-system-packages -e .

# Create working directory
RUN mkdir -p /var/lib/vmware2scw/work

# Default entrypoint
ENTRYPOINT ["vmware2scw"]
CMD ["--help"]
