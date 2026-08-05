# Rebuilds the WebArena gitlab benchmark image from provenance-clear parts:
# the official gitlab-ce release plus a gitlab-backup tar and /etc/gitlab
# capture derived from the upstream docker-save tar by derive-backup.sh
# (the builder's prepare step — see image.toml [prepare]).
#
# The restore stage's inputs come from the `datasets` build context the
# builder passes; the final stage copies only the restored state, so the 19G
# backup tar never enters a shipped layer.

FROM gitlab/gitlab-ce:15.7.5-ce.0 AS restore
COPY --from=datasets etc-gitlab.tar.gz /tmp/etc-gitlab.tar.gz
COPY --from=datasets webarena_gitlab_backup.tar /var/opt/gitlab/backups/
COPY restore-stage.sh /restore-stage.sh
RUN tar xzf /tmp/etc-gitlab.tar.gz -C / \
    && chown git:git /var/opt/gitlab/backups/*.tar \
    && /bin/bash /restore-stage.sh

FROM gitlab/gitlab-ce:15.7.5-ce.0
COPY --from=restore /etc/gitlab /etc/gitlab
# One layer per staging bucket keeps every pushed layer under ~10G (GHCR's
# comfortable ceiling). Bucket count must match BUCKET_COUNT in restore-stage.sh.
COPY --from=restore /staging/bucket-00/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-01/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-02/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-03/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-04/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-05/ /var/opt/gitlab/
EXPOSE 8023
