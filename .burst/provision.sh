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

# --- swap: a backstop against the OOM killer, not more memory ---
#
# webarena/shopping was killed on run 31669103000 during `COPY --from=restore
# /staging/bucket-04/`: the kernel OOM-killed docker-buildx while dockerd held
# ~10.9G on a 16 GiB box. The build is the *designated* victim (the job's
# processes carry oom_score_adj 500, dockerd -500), which is what makes the
# failure clean — but it also means buildkit bloat can only ever present as a
# dead job, never as a trimmed daemon. A few GB of swap turns that kill into a
# slowdown, which on a suite run that costs hours is the better trade.
#
# Small on purpose: headroom for an overshoot of a few GB, not a licence to
# build out of swap. It lives on the root volume — the same gp3 that is this
# workload's measured bottleneck — so swapping is expensive, hence swappiness
# 10: a backstop under real anonymous-memory pressure rather than a routine
# reclaim target.
#
# Created at boot rather than baked, for two reasons: the AMI is baked on a
# small volume while burst.toml sizes the real one (750 GB) at launch, and an
# 8 GiB file baked in would bloat every AMI snapshot.
install -m 0755 /dev/stdin /usr/local/sbin/burst-swap <<'EOF'
#!/bin/bash
set -euo pipefail
# Ephemeral single-use VMs, so this only ever runs on a fresh root.
[ -e /swapfile ] || fallocate -l 8G /swapfile
chmod 600 /swapfile
mkswap /swapfile >/dev/null
# fallocate is instant and valid for swap on ext4, which is what the Debian 13
# cloud image roots on. If that ever changes to a filesystem whose extents
# swapon rejects ("it appears to have holes"), fall back to a written file
# rather than silently leaving the VM without the backstop.
if ! swapon /swapfile 2>/dev/null; then
  rm -f /swapfile
  dd if=/dev/zero of=/swapfile bs=1M count=8192 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
sysctl -w vm.swappiness=10 >/dev/null
swapon --show
EOF

# WantedBy=multi-user.target is what actually guarantees the ordering: burst
# delivers and starts burst-runner.service from user-data (src/payload.rs), so
# it starts after cloud-init, by which point this has long since run. The
# Before= lines are defensive — docker.service is baked and enabled, so that
# one is a real edge; burst-runner.service does not exist at bake time and the
# reference simply resolves at runtime.
#
# Deliberately NOT a Requires/BindsTo of either: if swap setup fails, the job
# should still run exactly as it does today rather than the VM refusing to work
# at all. A failure shows up in the console log.
cat >/etc/systemd/system/burst-swap.service <<'EOF'
[Unit]
Description=Enable a swapfile as an OOM backstop for image builds
After=local-fs.target
Before=docker.service burst-runner.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/burst-swap

[Install]
WantedBy=multi-user.target
EOF
systemctl enable burst-swap.service

# Fail the bake here rather than the first job: an AMI that cannot build is
# worth catching while the builder is still running. These also print the exact
# versions this AMI froze, which is the only record of them.
docker --version
docker buildx version
docker compose version
systemctl is-enabled docker
# `systemctl enable` above already rejects a malformed unit, so this is the
# assert. Deliberately not `systemd-analyze verify`: burst-runner.service is
# delivered by user-data at launch and does not exist during the bake, so
# verify would fail the build over a Before= reference that is correct.
systemctl is-enabled burst-swap.service
