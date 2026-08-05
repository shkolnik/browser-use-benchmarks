# VWA classifieds — TODO (deliberate stub, 2026-08-05)

Not buildable via either existing manifest kind yet, so this directory carries no
`image.toml` (discovery is by `image.toml` glob, so the stub is invisible to the driver).

Why it fits neither kind:

- Not `docker-save`: the upstream artifact (`classifieds_docker_compose.zip`,
  sha256 `25b560fd0e1d7a8944ab3bb21916ffa2fe63e6272316d22d7bc43da9e7c0bc24`, from
  https://archive.org/download/classifieds_docker_compose/classifieds_docker_compose.zip)
  is NOT a `docker save` tar. It is a compose project: `docker-compose.yml` + a mysql
  restore path (`mysql/init_db.sh`, `mysql/classifieds_restore.sql`,
  `mysql/osclass_craigslist.sql`, ~85MB of SQL) that pulls stock images and populates a
  database at first `up`.
- Not a clean `build` either: the runnable unit is two services (app + mysql) whose state
  comes from a runtime restore, not a Dockerfile build. Making it fit our
  one-image-per-service model means either (a) baking the restored mysql data into a
  custom image (a real build wave, needs design), or (b) a third manifest kind that
  wraps an upstream compose project.

The zip is already cached in `datasets/` (and extracted at `datasets/classifieds/`).
Decision on approach (a) vs (b) is deferred; revisit when VWA is actually scheduled.
