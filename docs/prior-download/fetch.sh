#!/usr/bin/env bash
# Benchmark env images: VWA trio (classifieds/shopping/reddit-withimg) + WA app-subset duo
# (shopping_admin/gitlab). Metis was live earlier today but is timing out right now — poll
# until it answers, then download sequentially with resume (-C -). Log everything.
set -uo pipefail
D=/home/agent/webarena-images
LOG=$D/download.log
BASE=http://metis.lti.cs.cmu.edu/webarena-images
FILES=(shopping_final_0712.tar shopping_admin_final_0719.tar gitlab-populated-final-port8023.tar)
echo "[$(date -u +%FT%TZ)] fetch.sh start" >> "$LOG"
# Phase 1: wait for the host (up to 24h, probe every 5 min)
up=0
for i in $(seq 1 288); do
  code=$(curl -sI --max-time 20 -o /dev/null -w '%{http_code}' "$BASE/shopping_final_0712.tar" || true)
  if [ "$code" != "000" ] && [ -n "$code" ]; then
    echo "[$(date -u +%FT%TZ)] host UP (probe HTTP $code) after $i tries" >> "$LOG"; up=1; break
  fi
  sleep 300
done
if [ "$up" != 1 ]; then echo "[$(date -u +%FT%TZ)] GAVE UP: metis unreachable for 24h" >> "$LOG"; exit 1; fi
# Phase 2: sequential downloads, resume-capable, 3 attempts each
fail=0
for f in "${FILES[@]}"; do
  code=$(curl -sI --max-time 30 -o /dev/null -w '%{http_code}' "$BASE/$f" || true)
  echo "[$(date -u +%FT%TZ)] $f HEAD=$code" >> "$LOG"
  if [ "$code" = "404" ]; then echo "[$(date -u +%FT%TZ)] $f NOT FOUND on mirror — name needs correcting" >> "$LOG"; fail=1; continue; fi
  ok=0
  for a in 1 2 3; do
    curl -fL -C - --retry 5 --retry-delay 30 --max-time 21600 -o "$D/$f" "$BASE/$f" >> "$LOG" 2>&1 && { ok=1; break; }
    echo "[$(date -u +%FT%TZ)] $f attempt $a failed (will resume)" >> "$LOG"; sleep 120
  done
  sz=$(stat -c%s "$D/$f" 2>/dev/null || echo 0)
  echo "[$(date -u +%FT%TZ)] $f done=$ok size=$sz" >> "$LOG"
  [ "$ok" = 1 ] || fail=1
done
echo "[$(date -u +%FT%TZ)] fetch.sh COMPLETE fail=$fail; disk: $(df -h / | tail -1)" >> "$LOG"
exit "$fail"
