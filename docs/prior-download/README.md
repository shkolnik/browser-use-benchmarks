# Recovered prior-download artifacts (2026-08-05)

Salvaged from `~/webarena-images/` on the build sandbox — the July 2026 one-off download effort
whose docker images were later lost to a cleanup. The archives themselves live in the git-ignored
`datasets/` cache; these files preserve the hard-won metadata:

- `fetch.sh` — metis.lti.cs.cmu.edu mirror leg (WebArena app tars); host was flaky, script polls
  up to 24h then resumes with `curl -C -`.
- `fetch-ia.sh` — archive.org leg (VWA classifieds zip + postmill/reddit tar).
- `md5.expected` / `md5.actual` — upstream md5s for the two archive.org files (both verified).
- `sha256.computed` — sha256 of every archive, computed at salvage time; feeds future
  `image.toml` manifests.
- `*.log`, `shopping-retry.sh` — download session logs kept for provenance.

⚠️ The big `.tar`s are **upstream `docker save` images** (shopping, shopping_admin, gitlab,
postmill), NOT raw datasets — the WebArena "build" step will be load/re-tag/push (or a thin
Dockerfile layered on top), not a from-scratch build. `classifieds/` +
`classifieds_docker_compose.zip` are VWA's classifieds service (compose-based).

Known upstream URLs:
- http://metis.lti.cs.cmu.edu/webarena-images/{shopping_final_0712,shopping_admin_final_0719,gitlab-populated-final-port8023}.tar
- https://archive.org/download/classifieds_docker_compose/classifieds_docker_compose.zip
- https://archive.org/download/postmill-populated-exposed-withimg/postmill-populated-exposed-withimg.tar
