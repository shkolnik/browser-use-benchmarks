# webarena/wikipedia — kiwix-serve over the pinned English Wikipedia ZIM

The one fleet service that is upstream's own image used as-is: `kiwix-serve` 3.3.0 is a published
openZIM release and the archive is a published Kiwix dump, so nothing here needs a provenance
rebuild. What this image changes is **delivery** — upstream bind-mounts the 88.7 GiB archive from
the host, this bakes it in so the benchmark is one pullable artifact like the rest of the fleet.

That single change is where all the difficulty lives, and it is not where you would expect.

## Splitting a ZIM is free for articles and NOT free for search

The archive cannot be one `COPY`: a layer that size is far past any registry's ceiling. So it ships
as parts, using libzim's own `.zimaa`/`.zimab`/… split-archive convention — kiwix-serve is handed a
`<stem>.zim` path with no file at it and reassembles the parts itself.

**libzim hands Xapian a file descriptor plus an offset.** It can only do that when an index's blob
lies entirely inside ONE part file. Cut it, and the archive keeps serving every article perfectly
while search breaks — which is exactly the shape of bug this fleet exists to catch, because the
image looks fine.

The two failure modes are not equally loud:

| index | if a part boundary cuts it | how it presents |
|---|---|---|
| `X/fulltext/xapian` | full-text search dies | **loud** — HTTP 404, "The fulltext search engine is not available for this content" |
| `X/title/xapian` | suggestions degrade | **silent** — `/suggest` still answers **200**, having fallen back from Xapian relevance ranking to an alphabetical prefix scan |

The silent one is the dangerous one. Measured on the same archive, `/suggest?term=Ger`:

- index whole: `GERMANY`, `Amazon Germany`, `German infrastructure`, `Stefani Germanotta`
- index cut:   `GerMany`, `Geramny`, `German (country)`, `German Federal Republic`

Both are 200s full of plausible titles. Only a byte-diff against the unsplit archive tells them
apart, which is why `split-zim.sh` asserts containment rather than trusting a page to look right.

### The evidence

kiwix-serve 3.3.0, 2026-08-08, part COUNT held constant at 2 so the boundary was the only variable:

| boundary | fulltext search | article |
|---|---|---|
| INSIDE the fulltext index | 404, "not available" | 200 |
| just BEFORE it | 200 | 200 |

Boundaries are therefore **placed, not spaced**. `zim_layout.py` reads the ZIM's header, dirent and
cluster tables — a few KB, no scan — to find each index's byte extent, and pulls any boundary that
would land inside one back to that index's start. Parts need not be equal size for this to work:
verified with a deliberately lop-sided 7 MiB / 190 MiB / 125 MiB split whose article bytes *and*
search page were byte-identical to the unsplit archive.

### Keep the part count low

Independently of containment, search goes unavailable once an archive is split into too many parts.
Measured on a 332 MB fixture with every index provably whole: **80 parts works, 97 does not**, and
raising the container's `nofile` limit to 65536 does not change it — so it is internal to libzim,
not a file-descriptor ceiling. The real archive uses 11 parts, far below the bound, but a future
part-size change should not cross it blindly.

## The fulltext index is bigger than a layer, and what this image does about it

Measured extents in `wikipedia_en_all_maxi_2022-05.zim` (95,199,730,590 bytes):

| entry | bytes | GiB | extent |
|---|---|---|---|
| `X/title/xapian` | 3,158,687,744 | 2.94 | 74,440,368,308 .. 77,599,056,052 |
| `X/fulltext/xapian` | 16,006,144,000 | **14.91** | 77,599,387,200 .. 93,605,531,200 |

A part is a file, and a file lives in exactly one layer. So the fulltext index forces a **14.91 GiB
layer**, against the 10.003 GiB rung `docs/registry-limits.md` proved and GitHub's documented 10 GB
GHCR ceiling. Compression does not rescue it — the index is mostly incompressible:

- gzip -6, mean of 12 samples spanning the index: **0.8026** → 11.96 GiB
- ~60% of the index measures 0.945 on its own, so the mean cannot improve much
- even a hypothetical 0.70 lands at 10.43 GiB — still over

**So no part size can make this archive publishable with working search:** one part *is* one index,
and that index is over the ceiling. That is arithmetic, so no oversized test push was spent reaching
it.

The layout the archive *wants*, from the measured extents (`zim_layout.py boundaries <zim> 9G`):

```
part  0..6   9.00 GiB each
part  7      6.33 GiB
part  8      2.94 GiB   <- title index, whole
part  9     14.91 GiB   <- fulltext index, whole   ** the only part over the ceiling **
part 10      1.48 GiB
```

Every part but one fits. The one that does not is split **once more, along a different axis**: not
into more ZIM parts (which would cut the index) but into `.partNN` sub-files that mean nothing to
libzim. `entrypoint.sh` concatenates them back into `part 9` before kiwix-serve opens the archive,
so what libzim sees is exactly the layout above.

> ⚠️ **And it still does not restore search on this archive — measured, 2026-08-08.** See
> "Containment is not enough at 88.7 GiB" below. The machinery above works exactly as described and
> is verified; what it buys is a publishable layout, NOT working full-text search.

```
wikipedia_en_all_maxi_2022-05.zimaj.part00   7.45 GiB  ─┐ concatenated at container start
wikipedia_en_all_maxi_2022-05.zimaj.part01   7.45 GiB  ─┘ into ...zimaj (14.91 GiB)
```

What that costs, stated plainly:

- **~15 GiB in the container's writable layer**, on top of the image. The sub-files sit in a
  read-only layer and cannot be reclaimed by deleting them.
- **24 s**, measured on the real part (~640 MiB/s). Note this step is skipped entirely when the
  finished archive is already present — see `assemble-zim` below, which checks that first precisely
  so this cost is not paid to build an intermediate nothing reads.

Two alternatives were weighed and not taken. Pushing the 14.91 GiB layer as-is is **untested**: the
probe built for it died at `permission_denied: create_package` before a single byte was uploaded, so
nothing is known about whether GHCR would take it. Bind-mounting the archive from the host is what
upstream WebArena does, and it works, but it gives up the property this image exists for — that a
benchmark is one `docker pull`.

Enabling this image in CI is not an action, it is the *absence* of an exclude line in `build.yml`'s
discover step, so merging this directory to main is what turns it on.

## Containment is not enough at 88.7 GiB — the model does not survive its own scale test

Everything above about *where* to cut was established on a 332 MB fixture, where it is exactly
right: cut the index and search 404s, cut just before it and search is byte-identical to the whole
archive. On the real archive it is **not sufficient**. Measured against the whole file served side
by side (`/wikipedia_en_all_maxi_2022-05/...`, kiwix-serve 3.3.0, same host):

| layout | articles | `/search` | `/suggest` |
|---|---|---|---|
| whole 88.7 GiB file | 200 | **200**, 26,160 B | Xapian ranking |
| 10 uniform 9 GiB parts (index cut) | identical | 404 | degraded |
| **11 placed parts, both indexes provably whole** | **identical** | **404 "Fulltext search unavailable"** | **degraded** |

The third row is the one that refutes the model. Ruled out, by running:

- **not corrupt bytes** — the reassembled 14.91 GiB part hashes identical to the source region
  `77,599,387,200..93,605,531,200` (sha256 `e1e42aa08b336ac8e8437c7b...`), and every part is a
  contiguous slice whose sizes sum to the source exactly;
- **not the `.partNN` files confusing part enumeration** — moved out of the directory, container
  restarted with exactly eleven `.zimaa`..`.zimak` files present: still 404;
- **not the boundary** — `X/fulltext/xapian` occupies part 9 exactly, end to end, and
  `X/title/xapian` sits whole inside part 8, both re-derived from the files actually written.

`/suggest` degrades the same silent way as before: 200, plausible, alphabetically ordered
(`Ger`, `Ger "Redser" O'Grady`, `Ger (Hasidic dynasty)`) instead of ranked. A check that asserted
"200 and non-empty" would call this working.

Two split layouts of this archive now lose search, one of them with every index whole. The small
fixture shows splits *can* preserve search, so the difference is scale, not principle — the exact
libzim bound is not established here, and this document does not claim one. What is established is
the part that matters for shipping: **no boundary placement recovers search for this archive.**

That leaves three ways forward, and the choice is a fidelity-vs-footprint trade rather than a
technical unknown:

- **ship without full-text search**, loudly documented — cheapest, and WebArena's wiki tasks use
  search, so it is a real fidelity loss;
- **bind-mount the archive** as upstream WebArena does — search works, one-pull is given up;
- **reassemble the whole 88.7 GiB archive at boot** from the parts — **implemented on this branch
  and measured end to end** (below).

## Whole-archive reassembly at boot: measured, not inferred

`entrypoint.sh` joins the parts back into `<stem>.zim` before kiwix-serve opens anything. Run against
the real archive's ten parts, with the whole file served beside it from `datasets/` for comparison:

| endpoint | whole file | reassembled | |
|---|---|---|---|
| landing page | 200 | 200 | **byte-identical**, 19,396 B |
| `A/Germany` | 200 | 200 | **byte-identical**, 530,934 B |
| `/search?pattern=germany` | 200 | 200 | **byte-identical**, 26,160 B |
| `/suggest?term=Ger` | 200 | 200 | **byte-identical**, 1,349 B |

Full fidelity, including the Xapian ranking that the split layout silently lost (its degraded
`/suggest` was 4,094,669 B of alphabetical prefix matches against this 1,349 B).

The join is also provably lossless in the other direction: concatenating the parts yields sha256
`f12163513307893c87fd75009b1d61677bae675627eaadf4cb0fa63953eea021` over 95,199,730,590 bytes — which
is the **pinned upstream sha256 in `image.toml`**, so the container reconstructs exactly the bytes
this image's manifest verifies.

What it costs, measured rather than projected:

- **~89 GiB in the container's writable layer**, on top of the ~89 GiB image. The parts live in
  read-only layers, so deleting them after the join reclaims nothing. A running container is
  therefore around 178 GiB of disk.
- **632 s on first start** — about 144 MiB/s, not the ~640 MiB/s a single part reaches, because the
  join reads and writes the same disk at once. A projection from the single-part rate would have
  said ~140 s and been wrong by 4.5x, which is why the `HEALTHCHECK` start period is 3600 s.
- A restart of the same container is free: an archive already whole at the right size is skipped.

### Paying the 632 s once, instead of once per container

Where the joined archive lands decides how often it is paid, so the image keeps the two directories
apart: **parts ship at `/zim-parts`, the archive is assembled into `/zim`**, which holds nothing in
the image.

- **Default** — `/zim` is the container's writable layer, so the join is once per *container*.
  `docker restart` is free; `docker run` again pays it in full.
- **Named volume on `/zim`** — paid once for the life of the volume, however many containers come
  and go. The join is a documented command of its own, `assemble-zim`, so that cost can be spent
  deliberately right after `docker pull` rather than on a first boot somebody is waiting for:

  ```sh
  docker volume create wiki-zim
  # prepare it once. No HTTP_HOST/HTTP_PORT needed — building an archive has
  # nothing to do with how clients reach the server.
  docker run --rm -v wiki-zim:/zim --entrypoint assemble-zim \
      ghcr.io/shkolnik/webarena-wikipedia:latest
  # every later run starts in seconds
  docker run -d -e HTTP_HOST=localhost -e HTTP_PORT=8888 -p 8888:80 \
      -v wiki-zim:/zim ghcr.io/shkolnik/webarena-wikipedia:latest
  ```

  The entrypoint runs `assemble-zim` too, so a plain `docker run` still needs no ceremony; the script
  is idempotent and exits immediately when the archive is already whole.

The volume must go on `/zim` and **never** on `/zim-parts`: Docker seeds an empty named volume from
the image's contents at that path, so a volume over the parts would copy 88.7 GiB before the join
wrote another 88.7 GiB into the same volume. Separating the directories makes that unreachable
rather than merely documented.

Verified on a purpose-built test image whose third part was itself split into `.partNN` sub-files,
so both join steps ran: `assemble-zim` prepared a volume with no HTTP variables set; running it again
did nothing; a container booting on that volume did nothing and served; and a plain container with no
volume assembled from scratch as before. All three serving paths returned byte-identical article,
search and suggest responses.

That test also caught a defect worth keeping fixed: the check for "already assembled" has to happen
**before** the sub-file step, not after. Those intermediates land in the container's writable layer,
which is fresh for every `docker run`, so a container booting on an already-prepared volume was
rebuilding the 14.91 GiB fulltext part every time — spending the disk and the minutes to produce a
file nothing then reads. `assemble-zim` now derives the expected total from the parts without joining
anything, and exits early.

### The copy can be avoided entirely with FUSE — at a price this image declines to pay

The join exists because libzim wants one file. It does not need one file *on disk* — only one file in
the namespace it opens. A ~50-line FUSE filesystem that presents the parts as a single virtual file
gives libzim exactly that, copying nothing.

**Tested, and it works.** A concat FUSE fs over three parts of the 332 MB fixture, with kiwix-serve
opening the virtual path: article, `/search` and `/suggest` all **byte-identical** to the whole file
(747,052 / 21,852 / 1,722 B). Full-text search included — which follows, because libzim sees one file
and the split code path that loses search never runs. Single-file access at 95 GB is already proven
(the whole archive serves search fine), so the risk here is FUSE's, not libzim's.

Two reasons it is not what ships:

- **Privilege.** `--device /dev/fuse` and `--cap-add SYS_ADMIN` are not enough: the mount is refused
  until `--security-opt apparmor:unconfined --security-opt seccomp=unconfined` are added too. That is
  a near-root capability plus the removal of both sandbox layers, on every container running this
  benchmark, to save disk. For an image whose whole point is being trivial and safe to run, that is
  the wrong trade — and the wrong trade to make silently.
- **Unmeasured at scale.** Latency was identical on the fixture (search ~155 ms both ways), but that
  fixture fits entirely in page cache after one warm request, so it barely exercises FUSE at all.
  Random access across a 14.91 GiB Xapian index with a cold cache, through a userspace process, is
  the case that matters and it has **not** been measured. Treat the fixture numbers as evidence of
  correctness only, never of performance — this document already made the mistake once of trusting a
  332 MB result about this archive.

Worth revisiting if disk, not privilege, is ever the binding constraint; a C implementation would
also remove the Python process from the read path.

### Sizes, so the two numbers do not get confused

- **image: ~88.8 GiB** — the parts (95,199,730,590 B) plus a 102 MB base. ZIM payloads are already
  compressed, so a layer barely shrinks on push.
- **a running container: ~177 GiB** — the image plus the joined second copy, unless that copy lives
  in a shared named volume, in which case it is 88.7 GiB once per host rather than per container.

## Ports and the health check

Same published-vs-listen asymmetry as reddit. `start.sh` in the base image hardcodes
`kiwix-serve --port=80`, so the container always listens on 80 and compose publishes 8888
(WebArena's port). `HTTP_PORT` is the **published** 8888.

`HTTP_HOST`/`HTTP_PORT` are required and have no defaults, per `docs/service-contract.md`. Nothing
here is rewritten from them — kiwix-serve emits only relative links (verified: no self-referencing
absolute URL on the welcome page, an article, or a search page) — but every image in this fleet
answers "which address do clients use" the same way.

The `HEALTHCHECK` uses `wget`, not the fleet's usual curl, because this Alpine base ships no curl.
Two cautions, both checked in-container:

- busybox wget exits non-zero on a plain 404, but **following a redirect it prints the error and
  still exits 0** — so the probe must hit a path that answers 200 in one hop. The landing page does
  (zero redirects, 19,396 bytes).
- it tries `::1` first for `localhost`, and kiwix-serve binds `0.0.0.0:80` (IPv4 only), so
  `localhost` gets ECONNREFUSED. The probe uses **127.0.0.1**.

## No GHCR derived cache

Unlike every other prepare script in this fleet. Those derive small artifacts from a huge tar, so
caching turns a ~6 h metis fetch into a fast pull. Here the outputs are a byte-for-byte partition of
the input: caching them would trade an 88.7 GiB download for an 88.7 GiB download while permanently
doubling this benchmark's GHCR footprint. On a miss we re-fetch from archive.org, which is fast and
range-resumable.
