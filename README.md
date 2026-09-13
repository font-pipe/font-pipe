# Font pipeline

Turns a list of `google/fonts` families plus a folder of hand-added FOSS
fonts into a directory of subsetted `.woff2` files and a `manifest.json`,
built by a GitHub Action, published to a separate repo you can point a CDN
at. See `font-repo-writeup.md`-style background aside — this repo *is* the
implementation of that pipeline.

## The two decisions you asked about

**1. Fetch google/fonts automatically, or load fonts into the repo?**
Both, split by source:
- `google-fonts-families.txt` in *this* repo lists which `google/fonts`
  families to pull (e.g. `ofl/oswald`). The workflow sparse-checks out only
  those folders from `google/fonts` fresh on every run — add a line, push,
  and it's in the next build. You never store Google's font binaries in
  this repo.
- `manual-fonts/` in *this* repo is where you drop fonts from anywhere
  else (Velvetyne, League of Moveable Type, a foundry's own repo). Those
  *are* committed here, because there's no upstream index to re-fetch from
  and no `METADATA.pb` to trust — see `manual-fonts/README.md`.

Both paths run through the same pipeline and land in the same
`manifest.json`, distinguished by a `source` field per family.

**2. Where does the output go?**
A separate repo, pushed to via a fine-grained PAT stored as a secret here —
per your instinct. Concretely: `OUTPUT_REPO` (a repo variable, e.g.
`yourname/font-repo-output`) and `DEPLOY_REPO_TOKEN` (a secret). The
workflow checks out that repo with the token, rsyncs the freshly-built
`output/` over it, and commits+pushes only if something changed. That
output repo is what you'd point Cloudflare Pages / R2 at — see "Publishing
further" below.

## One-time setup

1. **Create the output repo** (e.g. `font-repo-output`) under your account,
   with at least one commit on its default branch (an initial README is
   fine — `actions/checkout` needs a branch to exist).
2. **Create a fine-grained PAT**: GitHub → Settings → Developer settings →
   Fine-grained tokens → generate one scoped to *only* the output repo,
   with **Contents: Read and write** permission. Nothing else needed.
3. **In *this* repo** (Settings → Secrets and variables → Actions):
   - Add secret `DEPLOY_REPO_TOKEN` = that PAT.
   - Add variable `OUTPUT_REPO` = `yourname/font-repo-output`.
4. Edit `google-fonts-families.txt` to the families you want (browse
   [github.com/google/fonts](https://github.com/google/fonts) for the
   `ofl/<name>`, `apache/<name>`, or `ufl/<name>` path), and/or drop fonts
   into `manual-fonts/` per its README.
5. Push to `main`, or run the "Build fonts" workflow manually from the
   Actions tab. It also re-runs weekly on its own, to pick up upstream
   fixes to families you've already listed.

## What actually gets built

For every family, whichever source it's from:
```
output/
  <slug>/
    <slug>-<style>-<weight>-<subset>.woff2   # one file per (style, weight, subset)
    OFL.txt | LICENSE.txt | UFL.txt          # whatever the source actually shipped
  manifest.json
```
`manifest.json` is a `{"generatedAt", "families": [...]}` object; each
family has `id`, `family`, `license`, `source`, `category`, `subsets`,
`subsetLabels` (human-readable, for a browse UI), and `files` — each file's
`path`, `weight` (a single value or a `"min max"` range for variable
fonts), `style`, `unicodeRange`, and `stretch` (only present when the font
has a `wdth` axis). This is exactly what the `FontFace`-per-`unicodeRange`
loading approach from earlier in the project expects.

Filenames always include the weight (`oswald-normal-200-700-latin.woff2`,
`chewy-normal-400-latin.woff2`) even though the writeup's early sketch
didn't — that's deliberate: plenty of real families (especially
manually-added ones) ship separate static files per weight rather than one
variable file, and without the weight in the name those would collide.

## Notes on specific choices

- **google/fonts families** get their name/license/subsets/weights from
  `METADATA.pb` (Google's own CI already validated it against the actual
  font). Weight for variable fonts still comes from the font's `fvar` axis
  range, not METADATA's single `weight` field, since that's the accurate
  CSS `font-weight` range.
- **Manual fonts** have no `METADATA.pb`, so weight/style/axes are read
  from the font's own OpenType tables, and script/language coverage is
  *detected* per file with `gfsubsets` (Google's own subset/codepoint
  definitions, which work on any font, not just Google's) — same technique
  Phase 2c in the writeup describes.
- **License handling**: google/fonts families are trusted based on which
  directory they came from (`ofl/`, `apache/`, `ufl/`) — that's the actual
  ground truth, cross-checked against `METADATA.pb`'s own license field.
  Manual fonts are checked against an explicit allowlist in
  `scripts/build.py` (`MANUAL_LICENSE_ALLOWLIST`); anything else is skipped
  with a warning in the build log, never guessed at.
- **The `menu` subset is skipped everywhere.** It's Google's pseudo-subset
  for rendering a family's name in their own font picker (just the glyphs
  of the family name), not a real character-set — irrelevant to serving
  document text, and `gfsubsets` doesn't even define it for non-Google
  fonts.
- **A real bug in `gfsubsets` is worked around explicitly.** The installed
  version's `CodepointsInSubset(subset)` (non-`unique_glyphs` path) returns
  a *reference* to its own internal memoized set and then mutates it in
  place with `|=`. Call it enough times in one process — e.g. once per
  family in a batch job exactly like this one — and subsets silently start
  absorbing codepoints from other subsets processed earlier in the same
  run, corrupting both later subsetting *and* later detection. This was
  caught by actually running the pipeline against several real families in
  sequence and noticing a manually-added, latin-only test font get
  detected as covering Cyrillic and Greek. `scripts/build.py` has
  `safe_codepoints_in_subset()`, which replicates the library's intended
  merge logic without touching its cache. If a future `gfsubsets` release
  fixes this upstream, the workaround is harmless dead-weight, not a
  liability.
- **Fontbakery QA (writeup Phase 6) is opt-in**, via the `run_qa` input
  when running the workflow manually (Actions tab → Run workflow). It's
  genuinely optional and non-blocking (`continue-on-error: true`) — it logs
  warnings, it doesn't stop a build or publish. Left off by default because
  it pulls in a large dependency tree for something that only matters when
  you're unsure about a new manual-font submission.

## Publishing further

The output repo is a plain directory of static files — point whatever host
you like at it:
- **Cloudflare Pages**: simplest is connecting Pages directly to the output
  repo via its own GitHub integration (Pages dashboard → "Connect to Git").
  It'll redeploy on every push this workflow makes, with zero extra tokens
  in this pipeline. Watch the 20,000-files-per-deployment cap if your
  family count grows large — split into multiple Pages projects (e.g. by
  first letter, or by license) before you hit it, not after.
- **R2 / any S3-compatible store**: add a step to the workflow (after the
  build step) running `aws s3 sync output/ s3://your-bucket/ --endpoint-url=...`,
  with credentials as additional secrets. Not included here since you
  didn't ask for it, but it's a small addition if you'd rather skip the
  output-repo hop entirely.

## CJK

Per the writeup: subset detection runs the same way for every family
regardless of script, but CJK fonts aren't meaningfully shrunk by
`unicode-range` splitting the way alphabetic scripts are — a subsetted CJK
file is still routinely multiple MB. This pipeline doesn't special-case
that yet (no `"heavy": true` flag in the manifest). Worth adding to
`build.py` before you point it at any CJK family, per the writeup's own
caveat.
