#!/bin/bash
# Runs in the Dockerfile's own "audited" stage, AFTER restore-stage.sh commits
# its RUN layer — so a failure here re-runs only this cheap check, not the
# ~restore (SPEC.md "Lesson to apply"; ../shopping lost two builds to this
# living inside the restore RUN instead).
set -euo pipefail

MAGE=/opt/magento

echo "=== ownership audit: everything a service reads at runtime ==="
# Root-run maintenance tooling drops root-owned metadata files in the datadir
# (Debian's install-time debian-NN.flag, mariadb-upgrade's mysql_upgrade_info).
# They are maintenance metadata, not data mariadbd serves, but normalize them
# so the audit below stays a zero-tolerance check.
chown mysql:mysql /var/lib/mysql/debian-*.flag /var/lib/mysql/mysql_upgrade_info 2>/dev/null || true
bad=$(find /var/lib/mysql ! -user mysql -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-mysql-owned files in datadir: $bad" >&2; exit 1; }
bad=$(find /var/lib/elasticsearch ! -user elasticsearch -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-elasticsearch-owned files in ES data: $bad" >&2; exit 1; }
bad=$(find "$MAGE" ! -user app -print 2>/dev/null | head -5)
[ -z "$bad" ] || { echo "non-app-owned files in magento tree: $bad" >&2; exit 1; }
echo "ownership audit OK"
