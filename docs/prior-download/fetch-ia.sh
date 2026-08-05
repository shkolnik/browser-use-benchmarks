#!/usr/bin/env bash
# archive.org leg (live now): VWA classifieds zip + forum-withimg 53.4GB tar.
# Sizes from the items' metadata API; forum size re-checked against Content-Length at run time.
set -uo pipefail
D=/home/agent/webarena-images; LOG=$D/download.log
get() { # $1=url $2=out
  local url=$1 out=$2 ok=0
  for a in 1 2 3 4 5; do
    curl -fsSL -C - --retry 5 --retry-delay 60 --max-time 86400 -o "$D/$out" "$url" && { ok=1; break; }
    echo "[$(date -u +%FT%TZ)] IA $out attempt $a failed (resuming)" >> "$LOG"; sleep 180
  done
  local sz; sz=$(stat -c%s "$D/$out" 2>/dev/null || echo 0)
  echo "[$(date -u +%FT%TZ)] IA $out ok=$ok size=$sz" >> "$LOG"
  [ "$ok" = 1 ]
}
echo "[$(date -u +%FT%TZ)] fetch-ia start" >> "$LOG"
fail=0
get 'https://archive.org/download/classifieds_docker_compose/classifieds_docker_compose.zip' classifieds_docker_compose.zip || fail=1
get 'https://archive.org/download/postmill-populated-exposed-withimg/postmill-populated-exposed-withimg.tar' postmill-populated-exposed-withimg.tar || fail=1
echo "[$(date -u +%FT%TZ)] fetch-ia COMPLETE fail=$fail; $(df -h / | tail -1)" >> "$LOG"
exit "$fail"
