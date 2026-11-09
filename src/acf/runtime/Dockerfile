# syntax=docker/dockerfile:1
FROM node:22-trixie-slim
ARG CODEX_VERSION=0.166.0
ARG CLAUDE_VERSION=2.1.318
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 TERM=xterm-256color \
    GHIDRA_HOME=/opt/acf-tools/ghidra GHIDRA_INSTALL_DIR=/opt/acf-tools/ghidra \
    PATH=/opt/acf/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git openssh-client sudo bash tmux tini \
    python3 python3-pip python3-venv build-essential \
    jq ripgrep file unzip zip xz-utils procps iproute2 iputils-ping dnsutils \
    netcat-openbsd socat less nano vim-tiny \
    chromium xvfb x11vnc x11-utils openbox novnc python3-websockify \
    dbus-x11 xterm fonts-liberation fonts-dejavu-core libnss3-tools \
    openjdk-21-jdk-headless && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex@${CODEX_VERSION} @anthropic-ai/claude-code@${CLAUDE_VERSION} \
    && npm cache clean --force \
    && userdel node && rm -rf /home/node \
    && useradd -m -u 1000 -s /bin/bash agent \
    && printf 'agent ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/acf-agent \
    && chmod 0440 /etc/sudoers.d/acf-agent \
    && mkdir -p /workspace /opt/acf/project /run/acf /var/log/acf \
    && chown agent:agent /workspace
RUN apt-get update && apt-get install -y --no-install-recommends xdotool \
    xfce4-session xfce4-panel xfce4-settings xfdesktop4 xfwm4 thunar xfce4-terminal \
    && rm -rf /var/lib/apt/lists/*
COPY . /opt/acf/
RUN chmod 0755 /opt/acf/bin/* \
    && python3 /opt/acf/install_commands.py \
    && python3 /opt/acf/install_codex_config.py \
    && python3 /opt/acf/install_browser_policy.py \
    && python3 /opt/acf/install_novnc.py \
    && printf 'set -g mouse on\nset -g history-limit 50000\nset -g default-terminal "screen-256color"\n' > /etc/tmux.conf \
    && printf 'cd /workspace\n' >> /root/.bashrc \
    && printf 'cd /workspace\n' >> /home/agent/.bashrc
WORKDIR /workspace
EXPOSE 8080 8081 6080 6082
LABEL org.opencontainers.image.title="Agent Containment Facility" acf.runtime="1"
ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/opt/acf/bin/acf-boot"]
