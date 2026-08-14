#!/bin/bash
# Runs inside the "restore" build stage of the gitlab Dockerfile.
# Boots the omnibus services, restores the pinned gitlab-backup tar, shuts
# everything down cleanly, then partitions /var/opt/gitlab into /staging
# buckets small enough for the final stage to COPY as <=10G registry layers.
set -euo pipefail

BK=webarena
BUCKET_LIMIT_KB=$((8 * 1024 * 1024))  # 8G target keeps layers under GHCR's ~10G comfort zone
BUCKET_COUNT=6  # must match the COPY --from lines in the Dockerfile's final stage

# Move puma off 8080 BEFORE anything boots.
#
# Rails runs behind nginx here, and puma binds a TCP port of its own on the
# loopback for it. Omnibus's default for that is 8080 — and this is the one
# image whose nginx listener follows HTTP_PORT, so a deployment publishing on
# 8080 aims both at the same bind. nginx wins, puma dies with EADDRINUSE and
# runit restarts it forever. 8080 is the second most common HTTP port there is;
# spending it on an internal detail nobody sees is the wrong trade, so puma
# moves instead. entrypoint.sh reserves whatever this names, reading it back
# from gitlab.rb rather than hardcoding a constant that could drift from it.
#
# Before the wrapper boot, not after: the wrapper's first-boot reconfigure
# renders puma's config, and a port changed afterwards would leave the shipped
# tree disagreeing with gitlab.rb — the entrypoint would then reserve a port
# puma is not on, and wave through the one it is.
PUMA_PORT=18080
printf "\n# See entrypoint.sh: HTTP_PORT may not equal this.\npuma['port'] = %s\n" \
    "$PUMA_PORT" >> /etc/gitlab/gitlab.rb

echo "=== boot services ==="
/assets/wrapper &
WRAPPER_PID=$!
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8023/ || true)
  if [ "$code" = 302 ] || [ "$code" = 200 ]; then
    echo "up after ~$((i * 15))s"
    break
  fi
  [ "$i" = 90 ] && { echo "gitlab never came up" >&2; exit 1; }
  sleep 15
done

# HTTP-up is not boot-done: the wrapper's first-boot reconfigure (cinc-client)
# keeps running after puma answers, and restoring under it races rake tasks
# against a half-configured instance (seen live: cache:clear at boot+142s vs
# our restore at boot+127s). Wait for it to finish before touching services.
echo "=== wait for first-boot reconfigure to finish ==="
for i in $(seq 1 60); do
  pgrep -f cinc-client >/dev/null || break
  [ "$i" = 60 ] && { echo "first-boot reconfigure never finished" >&2; exit 1; }
  sleep 15
done

echo "=== restore ==="
gitlab-ctl stop puma
gitlab-ctl stop sidekiq
gitlab-backup restore BACKUP=$BK force=yes
gitlab-ctl reconfigure

# The setting is only half of it; the rendered config is what puma actually
# binds, and it is what ships. Assert rather than assume: a reconfigure that
# quietly ignored puma['port'] would leave 8080 in place, and the collision
# would come back on a deployment we could no longer reproduce here.
PUMA_RB=/var/opt/gitlab/gitlab-rails/etc/puma.rb
grep -q "bind 'tcp://127.0.0.1:${PUMA_PORT}'" "$PUMA_RB" || {
  echo "puma did not move to ${PUMA_PORT}; rendered config says:" >&2
  grep -n "^bind" "$PUMA_RB" >&2
  exit 1
}
echo "puma bound to ${PUMA_PORT} (8080 left free for the published side)"

echo "=== clean shutdown ==="
gitlab-ctl stop
# A quiesced Postgres matters: the final image must start from a cleanly
# shut-down data directory, not one that looks like a crash. gitlab-ctl
# status exits non-zero when the service is down (that's the state we WANT),
# so capture output first instead of piping under pipefail.
pg_status=$(gitlab-ctl status postgresql 2>/dev/null || true)
echo "$pg_status" | grep -q '^down' || { echo "postgresql still up: $pg_status" >&2; exit 1; }
kill "$WRAPPER_PID" 2>/dev/null || true

echo "=== trim state not worth shipping ==="
# The backup tar is not deleted here, and must not be: it is a read-only bind
# mount of the datasets context, so it occupies nothing in this stage, and
# unlinking a mount point fails outright rather than quietly — `rm -f` returns
# 1, which under `set -e` would end the build after the whole restore.
# Log CONTENT is not worth shipping; the log DIRECTORY TREE is load-bearing.
# `gitlab-ctl reconfigure` creates /var/log/gitlab/<name> for each service AND
# for non-service consumers (gitlab-rails, gitlab-shell), owned by the account
# that writes there. A pristine gitlab-ce ships only /var/log/gitlab/reconfigure
# and this image never reconfigures at boot, so whatever this stage deletes is
# gone for good. Deleting the tree wedges the image three ways, each verified
# live 2026-08-07: svlogd cannot start, so every service sits `down: log`;
# nginx refuses to run at all and binds nothing; and puma crash-loops on
# ENOENT for gitlab-rails/log/grpc.log. Recreating the directories afterwards
# is NOT equivalent — they come back root-owned and puma then fails the same
# way with EACCES.
find /var/log/gitlab -type f -delete
# The build's gitlab-ctl stop leaves a root-owned redis dump.rdb (written by a
# root-context save), which the shipped image's gitlab-redis user cannot read —
# redis then crash-loops on every fresh boot and the API 500s. It's only
# cache/queue state; drop it rather than chown it.
rm -f /var/opt/gitlab/redis/dump.rdb
# Prometheus's TSDB directory comes out of this stage root-owned, and runit runs
# prometheus as gitlab-prometheus (`-U gitlab-prometheus:gitlab-prometheus` in
# /opt/gitlab/sv/prometheus/run), so it panics on "Unable to create mmap-ed
# active query log" and runit restarts it forever. The boot-time reconfigure
# does not rescue this one the way it re-asserts ownership elsewhere in this
# tree — checked live 2026-08-08 on a booted container, where every other
# root-owned directory belongs to a root service (nginx, logrotate) or to one
# this image never runs (consul, registry, mattermost).
#
# It is worth fixing rather than disabling because it is invisible: the crash
# loop costs nothing an agent can see, so nothing else in this pipeline would
# ever report it — /explore and the API answer 200 throughout.
chown -R gitlab-prometheus:gitlab-prometheus /var/opt/gitlab/prometheus

echo "=== partition /var/opt/gitlab into staging buckets ==="
# Shared implementation: builder/stage-lib/partition-tree.py, reached through
# the `stagelib` build context. It preserves each recreated parent directory's
# owner and mode, which is load-bearing HERE and nowhere else in the fleet: the
# sibling images run one app user and can follow the partition with a blanket
# `chown -R`, while this tree belongs to git, gitlab-psql, gitlab-redis and
# gitlab-www at once. A root-owned git-data/repositories/@hashed is invisible in
# the build log and leaves gitaly unable to write, which reaches the smoke gate
# as nginx serving 502 over a dead upstream.
mkdir -p /staging
for i in $(seq 0 $((BUCKET_COUNT - 1))); do mkdir -p "$(printf '/staging/bucket-%02d' "$i")"; done
python3 /partition-tree.py "$BUCKET_LIMIT_KB" "$BUCKET_COUNT" /var/opt/gitlab /staging

echo "restore stage complete"
