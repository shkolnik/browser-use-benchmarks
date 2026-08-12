#!/bin/bash
# Appended to burst-actions' stock provisioning script and run as root during
# `burst bake`, so everything here is baked into the AMI rather than paid per
# job. Base is Debian 13 (trixie); the stock script has already created the
# `burst` job user by the time this runs.
set -euo pipefail

# Docker comes from Docker's own apt repo, not Debian's. Debian 13 ships buildx
# 0.13.1, which is three years behind and already cost us a job: with the plugin
# installed `docker builder prune` aliases to `docker buildx prune`, and 0.13
# rejects `--reserved-space` (added in 0.17) with exit 125 — see the cleanup
# step in .github/workflows/build.yml. The home runner tracks upstream, so this
# also stops the two runners drifting apart on the tool that does all the work.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
Architectures: amd64
EOF
apt-get update

# What each piece is for:
#
#   docker-ce/-cli    the daemon and CLI.
#   containerd.io     the runtime docker-ce depends on.
#   docker-buildx-plugin
#                     `bin/build` invokes `docker build --build-context
#                     datasets=… --build-context stagelib=…`
#                     (builder/docker.py). Named build contexts are a buildx
#                     feature — without the plugin the CLI rejects the flag and
#                     no image builds.
#   docker-compose-plugin
#                     `bin/build smoke` drives `docker compose … up --wait` plus
#                     config/ps/logs/exec. --wait needs >= 2.1.1.
#
# Unpinned deliberately: the point of this change is to track upstream, and the
# version that lands is recorded by the asserts below in the bake log. The AMI
# is the pin — a baked image keeps whatever it got until something edits this
# file and forces a rebake.
apt-get install -y \
  docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# The job user needs the daemon socket. Group membership is read at login, and
# the runner's session starts long after this, so no re-login dance is needed.
usermod -aG docker burst

# Fail the bake here rather than the first job: an AMI that cannot build is
# worth catching while the builder is still running. These also print the exact
# versions this AMI froze, which is the only record of them.
docker --version
docker buildx version
docker compose version
systemctl is-enabled docker
