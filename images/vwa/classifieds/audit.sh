#!/bin/bash
# Build-time audit, in its own layer so a failed assertion does not re-run the
# ~1h restore. Two independent checks, both fail-loud.
set -euxo pipefail

DB_NAME=osclass
DB_PASS=password

fail() { echo "audit: $*" >&2; exit 1; }

# ==========
# 1. The app tree reconstructs. app-tree.sha256 is the manifest of the upstream
# image's deployed tree, taken file-by-file from a running upstream container —
# so this asserts that Osclass's own release zip plus webarena-modifications.patch
# really does reproduce what the benchmark shipped. It caught nothing when it was
# written (the reconstruction was already verified by hand); it exists so that a
# drifting patch, a re-cut release zip, or a dropped file cannot pass silently.
#
# The restore stage has already moved the tree into staging buckets, so the
# manifest is checked against the buckets' merged view — which is also exactly
# what the final stage's COPY lines produce.
# ==========
cd /staging
actual=$(mktemp)
for b in bucket-*; do
  # -path './config.php', not -name: the app also ships
  # oc-includes/vendor/google/recaptcha/examples/config.php.dist, and a loose
  # match dropped it from both sides of an earlier comparison — a filter that
  # hides the same file from the expectation and the measurement agrees with
  # itself no matter what the build did.
  (cd "$b" && find . -type f -not -path './oc-content/uploads/*' -not -path './config.php' \
      -print0 | sort -z | xargs -0 -r sha256sum)
done | sort -k2 > "$actual"

expected=$(sort -k2 /app-tree.sha256)
if ! diff -q <(echo "$expected") "$actual" > /dev/null; then
  echo "audit: the rebuilt app tree does not match app-tree.sha256" >&2
  echo "--- first 20 differences (expected < / actual >) ---" >&2
  diff <(echo "$expected") "$actual" | head -20 >&2
  fail "app tree mismatch: $(diff <(echo "$expected") "$actual" | grep -c '^[<>]') differing lines"
fi
echo "audit: app tree matches app-tree.sha256 ($(wc -l < "$actual") files)"

# The photos are data, so they are counted rather than hashed. Floors, not
# equalities — measured at 84,148 per-item directories and 336,634 files.
dirs=$(find /staging/*/oc-content/uploads -mindepth 1 -maxdepth 1 -type d | wc -l)
files=$(find /staging/*/oc-content/uploads -type f | wc -l)
[ "$dirs" -ge 84000 ] || fail "oc-content/uploads has $dirs item directories, expected >= 84000"
[ "$files" -ge 330000 ] || fail "oc-content/uploads has $files files, expected >= 330000"
echo "audit: uploads = $dirs item directories, $files files"

# ==========
# 2. The database restored. Pins measured from the booted upstream stack, not
# read off the dump — an earlier guess from reading the dump had the seeded-item
# count wrong by three orders of magnitude.
# ==========
mysqld --user=mysql --datadir=/var/lib/mysql --skip-networking &
mysqld_pid=$!
for i in $(seq 1 60); do
  mysqladmin --socket=/var/run/mysqld/mysqld.sock ping >/dev/null 2>&1 && break
  [ "$i" = 60 ] && fail "mysqld never accepted connections"
  sleep 2
done

q() { mysql --socket=/var/run/mysqld/mysqld.sock -uroot -p"$DB_PASS" -N -B "$DB_NAME" -e "$1" 2>/dev/null; }

check() {
  local label=$1 expected=$2 actual
  actual=$(q "$3")
  [ "$actual" = "$expected" ] || fail "$label = $actual, expected $expected"
  echo "audit: $label = $actual"
}

check tables 39 "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME';"
check oc_t_item 84149 "SELECT COUNT(*) FROM oc_t_item;"
check oc_t_item_description 84152 "SELECT COUNT(*) FROM oc_t_item_description;"
check oc_t_item_resource 84149 "SELECT COUNT(*) FROM oc_t_item_resource;"
check oc_t_user 1 "SELECT COUNT(*) FROM oc_t_user;"
check oc_t_category 23 "SELECT COUNT(*) FROM oc_t_category;"
check max_item_id 84154 "SELECT MAX(pk_i_id) FROM oc_t_item;"
# The reset script deletes everything at or above 84143; if that boundary ever
# stopped selecting rows, task isolation would silently become a no-op.
check items_at_reset_boundary 12 "SELECT COUNT(*) FROM oc_t_item WHERE pk_i_id >= 84143;"

mysqladmin --socket=/var/run/mysqld/mysqld.sock -uroot -p"$DB_PASS" shutdown
wait "$mysqld_pid" || true
echo "audit: OK"
