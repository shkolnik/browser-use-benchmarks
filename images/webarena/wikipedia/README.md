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

```
wikipedia_en_all_maxi_2022-05.zimaj.part00   7.45 GiB  ─┐ concatenated at container start
wikipedia_en_all_maxi_2022-05.zimaj.part01   7.45 GiB  ─┘ into ...zimaj (14.91 GiB)
```

What that costs, stated plainly:

- **~15 GiB in the container's writable layer**, on top of the image. The sub-files sit in a
  read-only layer and cannot be reclaimed by deleting them.
- **~106 s on first start**, measured at the 144 MiB/s this host sustains under a parallel build.
  A restart of the same container is free — the size check skips a part that is already whole.

Two alternatives were weighed and not taken. Pushing the 14.91 GiB layer as-is is **untested**: the
probe built for it died at `permission_denied: create_package` before a single byte was uploaded, so
nothing is known about whether GHCR would take it. Bind-mounting the archive from the host is what
upstream WebArena does, and it works, but it gives up the property this image exists for — that a
benchmark is one `docker pull`.

Enabling this image in CI is not an action, it is the *absence* of an exclude line in `build.yml`'s
discover step, so merging this directory to main is what turns it on.

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
