# Rebuilds the WebArena gitlab benchmark image from provenance-clear parts:
# the official gitlab-ce release plus a gitlab-backup tar and /etc/gitlab
# capture derived from the upstream docker-save tar by derive-backup.sh
# (the builder's prepare step — see image.toml [prepare]).
#
# The restore stage's inputs come from the `datasets` build context the
# builder passes; the final stage copies only the restored state, so the 19G
# backup tar never enters a shipped layer.

FROM gitlab/gitlab-ce:15.7.5-ce.0 AS restore
COPY --from=stagelib partition-tree.py /partition-tree.py
COPY restore-stage.sh /restore-stage.sh
# Bind-mounted rather than COPYed: the 19G backup tar is read once, by `tar -xf`
# inside gitlab-backup, and never shipped. A COPY writes all of it into this
# stage's layer, where deleting it afterwards only records a whiteout.
#
# Mounted at /mnt and symlinked into the backups directory by the restore stage,
# NOT mounted there directly. A mount point cannot be unlinked, so an archive
# mounted under /var/opt/gitlab stays in the tree the partitioner splits and is
# refused as a 19G layer member. The symlink is removable; the mount is not.
#
# No `chown git:git` goes with it any more, and none is needed. The mount lands
# the archive root-owned 0644 (verified against buildkit), gitlab-backup opens
# it as git, and read permission is all it wants: Backup::Manager#unpack does
# File.exist? then `tar -xf`, extracting into the backups DIRECTORY, which is
# still writable. Nothing in the restore path writes to or unlinks the archive.
RUN --mount=type=bind,from=datasets,source=etc-gitlab.tar.gz,target=/tmp/etc-gitlab.tar.gz \
    --mount=type=bind,from=datasets,source=webarena_gitlab_backup.tar,target=/mnt/webarena_gitlab_backup.tar \
    tar xzf /tmp/etc-gitlab.tar.gz -C / \
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
# base image declares interval 60s / retries 5 and NO start_period, so a
# GitLab still coming up correctly is marked unhealthy — and `docker compose
# up --wait`, which the smoke gate runs, treats that as terminal, failing a
# perfectly good image mid-boot.
#
# 150s is measured, not guessed: this image boots to healthy in 61s (2026-08-08,
# a full boot including the ~28s entrypoint reconfigure — it ships a pre-enabled
# service tree rather than reconfiguring from scratch, which is what makes a
# minute possible). 150s is ~2.5x that. The grace period is a CEILING ON
# DIAGNOSIS, not a budget to spend: a broken image reports unhealthy only after
# it expires, and the 900s that used to sit here is why a gitlab that was dead
# on arrival took 1107s of CI to say so.
HEALTHCHECK --interval=30s --timeout=30s --start-period=150s --retries=5 \
    CMD /opt/gitlab/bin/gitlab-healthcheck --fail --max-time 10

# --chmod so the mode is a property of the BUILD, not of the checkout.
# Installed into every runit service directory by the entrypoint: gitlab's
# implementation of the fleet contract that a dead service fails the
# container (#73), using runit's own per-service hook.
COPY --chmod=755 runit-finish.sh /runit-finish.sh
# The other half of that contract: the entrypoint runs this in the background to
# decide when boot is over and the hooks go live — or to fail the container if
# boot never ends.
COPY --chmod=755 arm-services.sh /arm-services.sh
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# The entrypoint ends by exec'ing (or waiting on) runsvdir-start, which is what
# this image used to be given as a compose `command:`. That override is removed
# in compose.yml: a `command:` would replace this CMD wholesale and silently
# skip the HTTP_HOST/HTTP_PORT validation — the contract would be declared and
# never run.
CMD ["/entrypoint.sh"]
