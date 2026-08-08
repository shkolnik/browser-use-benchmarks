# shellcheck shell=bash
#
# The service set for the WebArena shopping appliance. Sourced by BOTH
# restore-stage.sh (build time) and entrypoint.sh (run time), after
# /run-services.sh — so the boot path validated in the build is the boot path
# that ships. That property is why this is one file and not two lists.
#
# Replaces conf/supervisord.conf. There is no supervisor daemon any more: if
# any of these exits, the container exits with its code (#73). Under
# supervisord every one of them was autorestart=true, which meant a crashed
# elasticsearch came back empty — search silently returning nothing while the
# healthcheck stayed green, on a fixture whose whole job is answering queries.
#
# Logs stay in /var/log/supervisor/<service>.log, written by a REDIRECT rather
# than a pipe (svc_start --log). A pipe would make the shell's $! the last
# command of the pipeline, so the supervisor would watch `cat` instead of the
# service and the contract would quietly stop holding.

svc_start_stack() {
  svc_start mariadb --user mysql --log /var/log/supervisor/mariadb.log -- \
    /usr/sbin/mariadbd --user=mysql
  svc_start redis --user redis --log /var/log/supervisor/redis.log -- \
    /usr/bin/redis-server /etc/redis/redis-shopping.conf
  # `env` rather than exporting these: it execs, so the pid stays the service's,
  # and ES's settings do not leak into every other service's environment.
  svc_start elasticsearch --user elasticsearch \
    --log /var/log/supervisor/elasticsearch.log -- \
    env ES_PATH_CONF=/etc/elasticsearch ES_TMPDIR=/tmp \
    /usr/share/elasticsearch/bin/elasticsearch
  # root master + app workers (standard fpm deployment): the image's pool ships
  # `;user = app` commented out (its normal entrypoint runs the master as app),
  # and a root master refuses to start without a pool user (exit 78, seen live
  # in the first restore-stage run) — the Dockerfile uncomments it.
  svc_start php-fpm --log /var/log/supervisor/php-fpm.log -- \
    /usr/local/sbin/php-fpm -F
}

# Deliberately separate: nginx starts only after base_url is set from
# HTTP_HOST/HTTP_PORT, or it would serve pages carrying the previous host's
# URLs. This is what `autostart=false` used to express.
svc_start_nginx() {
  svc_start nginx --log /var/log/supervisor/nginx.log -- \
    /usr/sbin/nginx -g "daemon off;"
}
