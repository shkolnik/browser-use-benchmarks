#!/bin/bash
# Appended to burst-actions' stock provisioning script and run as root during
# `burst bake`, so everything here is baked into the AMI rather than paid per
# job. Base is Debian 13 (trixie); the stock script has already created the
# `burst` job user by the time this runs.
set -euo pipefail

# docker.io alone is NOT enough for this repo, and both gaps fail at the start
# of the first job rather than here:
#
#   docker-buildx   `bin/build` invokes `docker build --build-context
#                   datasets=… --build-context stagelib=…` (builder/docker.py).
#                   Named build contexts are a buildx feature — without the
#                   plugin the CLI rejects the flag and no image builds.
#                   0.13 covers it; --build-context needs >= 0.8.
#
#   docker-compose  `bin/build smoke` drives `docker compose … up --wait` plus
#                   config/ps/logs/exec. In trixie this package IS compose v2
#                   (2.26.1), not the old Python v1. --wait needs >= 2.1.1.
apt-get install -y docker.io docker-buildx docker-compose

# The job user needs the daemon socket. Group membership is read at login, and
# the runner's session starts long after this, so no re-login dance is needed.
usermod -aG docker burst

# Fail the bake here rather than the first job: an AMI that cannot build is
# worth catching while the builder is still running.
docker --version
docker buildx version
docker compose version
