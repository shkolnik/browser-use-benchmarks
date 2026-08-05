#!/usr/bin/env bash
# Gentle background retry for the VWA shopping image via Google Drive.
# All other shopping sources are down: archive.org item is DARKED, metis host is
# DOWN. GDrive is quota-blocked for this large public file; the quota resets on a
# rolling ~24h basis, so retry every 2h until it succeeds (or give up after 36h).
set -u
cd /home/agent/webarena-images || exit 1
ID=1gxXalk9O0p9eu1YkIJcmZta1nvvyAJpA
OUT=shopping_final_0712.tar
LOG=shopping-download.log
GDOWN=/home/agent/webarena-images/.gdown-venv/bin/gdown
for i in $(seq 1 18); do
  if [ -f "$OUT" ]; then
    echo "[$(date -u +%FT%TZ)] shopping already present ($(stat -c%s "$OUT") bytes) — retry loop exiting" >> "$LOG"
    exit 0
  fi
  echo "[$(date -u +%FT%TZ)] shopping GDrive retry attempt $i/18" >> "$LOG"
  if "$GDOWN" "$ID" -O "$OUT.part" >> "$LOG" 2>&1 && [ -s "$OUT.part" ]; then
    mv "$OUT.part" "$OUT"
    echo "[$(date -u +%FT%TZ)] shopping download COMPLETE on attempt $i size=$(stat -c%s "$OUT")" >> "$LOG"
    exit 0
  fi
  rm -f "$OUT.part"
  echo "[$(date -u +%FT%TZ)] attempt $i failed (quota); sleeping 2h" >> "$LOG"
  sleep 7200
done
echo "[$(date -u +%FT%TZ)] shopping GDrive retry GAVE UP after 18 attempts (~36h). metis + archive.org also unavailable." >> "$LOG"
