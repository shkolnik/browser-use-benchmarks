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
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
RUN tar xzf /tmp/etc-gitlab.tar.gz -C / \
    && chown git:git /var/opt/gitlab/backups/*.tar \
    && /bin/bash /restore-stage.sh

FROM gitlab/gitlab-ce:15.7.5-ce.0
COPY --from=restore /etc/gitlab /etc/gitlab
# /opt/gitlab too, and not for the software — for the runit service tree.
# `gitlab-ctl reconfigure` is what ENABLES services, by writing /opt/gitlab/sv/<svc>
# and symlinking them into /opt/gitlab/service/. A pristine gitlab-ce ships that
# tree empty (service/ has no entries, sv/ has only sshd), because upstream's
# entrypoint reconfigures on every boot. This image doesn't: it runs
# runsvdir-start directly (webarena's documented command, and what keeps boot
# fast), so it must ship the enabled tree the restore stage built. Without this
# COPY the container comes up, supervises nothing, answers nothing, and
# `gitlab-ctl status` prints an empty list with exit 0 — seen live 2026-08-07.
#
# The whole directory rather than a curated list: reconfigure writes into five
# separate trees under it (sv/, service/, init/, etc/, embedded/{cookbooks,etc,
# nodes,service,ssl} — measured with `docker diff` on a 15.7.5 first boot). A
# hand-maintained subset of another project's internals is one upstream change
# away from being silently incomplete again, and this is the failure mode that
# produces a container which looks up and does nothing.
COPY --from=restore /opt/gitlab /opt/gitlab
# And the log tree, for the same reason: reconfigure creates a directory per
# service (plus gitlab-rails and gitlab-shell) with the right owner, the base
# image ships only /var/log/gitlab/reconfigure, and this image never
# reconfigures at boot. Without it svlogd cannot start, nginx will not run, and
# puma crash-loops opening gitlab-rails/log/grpc.log. Carries no log content —
# restore-stage.sh deletes the files and keeps the directories.
COPY --from=restore /var/log/gitlab /var/log/gitlab
# One layer per staging bucket keeps every pushed layer under ~10G (GHCR's
# comfortable ceiling). Bucket count must match BUCKET_COUNT in restore-stage.sh.
COPY --from=restore /staging/bucket-00/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-01/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-02/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-03/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-04/ /var/opt/gitlab/
COPY --from=restore /staging/bucket-05/ /var/opt/gitlab/
EXPOSE 8023

# Upstream's own healthcheck test, restated only to add a start period. The
# base image declares interval 60s / retries 5 and NO start_period, so any
# GitLab that takes longer than ~5 minutes to boot — all of them — is marked
# unhealthy while it is still coming up correctly. That matters beyond
# cosmetics: `docker compose up --wait`, which the smoke gate runs, treats a
# container going unhealthy as a terminal failure, so the gate would fail a
# perfectly good image mid-boot.
HEALTHCHECK --interval=30s --timeout=30s --start-period=900s --retries=5 \
    CMD /opt/gitlab/bin/gitlab-healthcheck --fail --max-time 10

# --chmod so the mode is a property of the BUILD, not of the checkout.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# The entrypoint ends by exec'ing (or waiting on) runsvdir-start, which is what
# this image used to be given as a compose `command:`. That override is removed
# in compose.yml: a `command:` would replace this CMD wholesale and silently
# skip the HTTP_HOST/HTTP_PORT validation — the contract would be declared and
# never run.
CMD ["/entrypoint.sh"]
