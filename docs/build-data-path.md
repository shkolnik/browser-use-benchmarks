# The build data path: measurements that decide the design

Every image in this fleet is mostly *data* — 19G to 116G of it per image, arriving as one
upstream archive and leaving as a set of registry layers. The machinery in between
(`[media]`, `builder/stage-lib/demux-media.py`, `builder/stage-lib/partition-tree.py`) is
shaped by a handful of properties of `tar`, `ADD` and buildkit that are easy to get wrong
and, in every case here, **fail silently**: the build goes green and the image is wrong.

Each section is a measurement, not a recollection. Where a number appears, it came from
running the thing.

## An audit of an extracted tree audits the extractor

This is the trap that motivated the whole document.

The obvious way to check a dataset is to extract it and walk the result — assert modes,
ownership, that nothing is world-writable, that nothing is setuid. That check is not
answering the question you think it is. `tar x` **transforms** what it writes:

- it applies the caller's **umask** to every entry it creates, so the mode you audit is the
  archive's mode masked by whatever the runner happened to have set;
- the kernel **drops setgid** when the extracting process is not a member of the entry's
  group, so a setgid directory can arrive without the bit and look clean;
- `--numeric-owner` decides whether `uid 101` means *uid 101* or means *whatever `101` maps
  to in the build host's `/etc/passwd`*. Without it, ownership is a property of the host.

So a passing audit means "the extractor produced something acceptable **on this host**",
which is a weaker claim than "the archive contains something acceptable", and the gap moves
with the runner. `demux-media.py` audits the **headers as they stream past** instead. Those
are the archive's own bytes: the same on every host, and an unsafe mode is refused rather
than quietly masked off on the way through.

The general form: *any check downstream of a transformation tests the transformation too.*
It is worth asking, of each assertion in a restore stage, which of the two it is really for.

## `du` is not the size of a layer

A bucket becomes a `COPY`/`ADD` layer, and **a layer is a tar**: 512 bytes of header per
entry, plus each payload padded up to a 512-byte block. `du -sk` reports *allocated blocks*,
a different quantity, and it is wrong in both directions. Measured on three 2,000-entry
trees:

| tree | `du -sk` | real tar | error |
|---|---|---|---|
| 2,000 × 100 B | 8,000 K | 2,010 K | `du` over by 4× (4 K per file) |
| 2,000 × 4096 B | 8,000 K | 9,010 K | `du` under by 512 B per entry |
| one 64 MB sparse file | **0 K** | 65,540 K | `du` under by the entire file |

`du --apparent-size` is not the fix either: it misses the headers and the padding, reporting
196 K for that first tree against a real 2,010 K.

Only the third row can actually lose a build, and that is the one to keep in mind: a sizing
pass that consults `du` can wave through a bucket that becomes an oversized layer, and the
registry says so at **push** time — after the whole image has been built.

`entry_blocks()` in `partition-tree.py` and `demux-media.py` predicts all three exactly.
Both err *high* where they must approximate (a hard link is charged as a full entry, a long
name is charged its `././@LongLink` header): a bucket that measures larger than it lands
packs slightly loose, while one that measures smaller is a layer the registry refuses.

## A zero-member tar is not an archive to docker

`ADD` auto-extracts a tar from the build context. A tar with **no members** is not
recognised as one, so `ADD` treats it as an ordinary file and drops a literal
`bucket-07.tar` into the destination directory. The build succeeds. The image is wrong in a
way nothing downstream looks at.

This is reachable whenever `max_buckets` is a ceiling with headroom — the spare buckets have
no data. The media path pads an unused bucket with the archive's **top-level directories**,
which already appear in other buckets and re-extract to the same thing. When the archive's
top level is all files there is nothing to pad with, and `demux-media.py` **refuses** rather
than emitting the empty tar (it names the `max_buckets` value that would work).

## `ADD --chown` is silent when it cannot resolve a name

Two halves, both worth knowing:

- **With no `--chown` flag at all**, `ADD` reproduces the archive's uid/gid *and* mode
  verbatim, and it does so **numerically** — buildkit reads the header's `uid`/`gid` and
  ignores its `uname`/`gname`. This is what lets an image ship a tree with more than one
  owner (`map-tile`'s cluster is `101:103` mode `0700` while the volume root's files are
  `1000:1000`), which a single `--chown` would flatten.
- **With a `--chown` naming an account the image does not have**, everything lands
  `root:root` and the build **exits clean**.

Both, from one build over an archive whose entry is uid 1001 mode `0700`, into an `alpine`
that has no such account:

```
with --chown=nosuchuser:nosuchgroup:  /a/tree root:root         700   <- build exit 0
without:                              /b/tree UNKNOWN:UNKNOWN   700
```

`UNKNOWN:UNKNOWN` *is* the numeric preservation: the uid came through intact into an image
with no name for it. The mode came through both times — `--chown` changes ownership only.

Both failure modes produce an image that builds, pushes and boots. Postgres refusing to
start on a data directory it does not own is the *first* sign, and it appears at run time.
An image whose ownership is load-bearing should assert it after the `ADD`s — that assertion
is the only thing standing between a future `--chown`, a re-captured archive, or a
name-resolving extractor and a shipped image that cannot start.

## `COPY --from` commits the whole archive; `rm` afterwards reclaims nothing

`COPY --from=datasets big.tar /tmp/big.tar` writes every byte into that stage's layer. A
later `rm -f /tmp/big.tar` records a **whiteout** in the next layer; the bytes stay. For
`map-nominatim` that single line was 124,774,901,760 bytes — over a fifth of the image's
measured 520.7G peak disk — to read 34.8G back out of it.

`--mount=type=bind,from=datasets,source=big.tar,target=/tmp/big.tar` reads it where it
already sits and commits nothing. Two consequences:

- A bind mount is **read-only**. Every paired `rm -f "$TAR"` has to go: unlinking a mount
  point returns 1, which under `set -e` ends the build *after* the entire restore has run.
- Build args **do** expand inside the mount flags (`source=${SRC}`), verified against real
  buildkit.

The exception is an archive that is genuinely final-stage payload — `wikipedia`'s zim parts
ship, so they are copied.

## `tar` seeks past members it is not extracting — if the archive is seekable

Naming a member means tar does not *read* the rest, provided `-f` points at a real file.
On an 8G archive whose tail is one huge member, extracting a small member from the front:

| input | elapsed |
|---|---|
| `tar -xf archive` (seekable) | **0.002 s** |
| `cat archive \| tar -x` (pipe) | **3.4 s** |

So `map-nominatim` reads the 34.8G it keeps, not the 116G it is given: walking the archive's
header chain shows `nominatim-data` occupying members 0–7220 (the first 34.77 GiB) and the
whole discarded `nominatim-flatnode` volume in three members after it.

The caveat is the useful part. Joining split parts through `cat`, or piping an archive for
any other reason, silently puts the skipped bytes back on the clock — the command still
works, and nothing in the log changes.

## `--checkpoint` counts records, not blocks

`tar --checkpoint=N` fires every N **records**, and a record is the blocking factor's
20 × 512 B = 10,240 bytes — not 512. `--checkpoint=100000` is therefore roughly 1.02 GB per
line, which is why it prints nothing at all on an archive of a few hundred megabytes.

`--checkpoint-action=echo='… %T after %ds'` gives tar's own progress: `%T` expands to bytes,
a human-readable size and a rate; `%d` is seconds elapsed.

## Why any of this is worth writing down

Every failure above is silent. None of them makes a build fail, and most of them make an
image that starts. The visible ones — a layer the registry rejects, a database that will not
open its data directory — surface at push time or at boot, which is to say after the
expensive part is over. The cheap place to catch them is a header, before anything is
written.
