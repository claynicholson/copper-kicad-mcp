# EDA-bench agent image on KiCad 10.
#
# Mirrors EDA-bench's own images/agent/Dockerfile (a Debian-family base + KiCad + ngspice + Python + CLI
# tools), but with KiCad 10.0.8 from KiCad's official PPA instead of Debian trixie's KiCad 9, because Copper
# only edits with KiCad 10+. Ubuntu 26.04 is used because the PPA builds 10.0.8 for it on both amd64 and
# arm64, so the image runs natively on x86 hosts and on Apple Silicon (the official kicad/kicad image is
# amd64-only).
#
# The image carries no Copper: the copper-claude-code agent installs it at setup time, so a baseline agent run
# on this same image sees exactly the same environment.
FROM ubuntu:26.04

ARG DEBIAN_FRONTEND=noninteractive
ARG KICAD_VERSION=10.0.8~ubuntu26.04.1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg software-properties-common \
    && add-apt-repository -y ppa:kicad/kicad-10.0-releases \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "kicad=$KICAD_VERSION" \
        "kicad-symbols=$KICAD_VERSION" \
        "kicad-footprints=$KICAD_VERSION" \
        "kicad-templates=$KICAD_VERSION" \
        curl \
        git \
        jq \
        ngspice \
        procps \
        python3 \
        python3-pil \
        python3-venv \
        ripgrep \
        sudo \
    && apt-mark hold kicad kicad-symbols kicad-footprints kicad-templates \
    && rm -rf /var/lib/apt/lists/*

# Global symbol/footprint library tables for root (Harbor runs the agent and the verifier as root), so
# kicad-cli and pcbnew resolve the stock libraries the way a desktop install does.
RUN v="$(kicad-cli -v | cut -d . -f 1,2)" \
    && mkdir -p "/root/.config/kicad/$v" \
    && cp /usr/share/kicad/template/*-lib-table "/root/.config/kicad/$v/" \
    && python3 -c "import pcbnew, PIL; print('pcbnew', pcbnew.GetBuildVersion(), 'pillow', PIL.__version__)"

WORKDIR /workspace
