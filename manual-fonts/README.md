# manual-fonts/

Fonts from FOSS sources other than `google/fonts` (Velvetyne, League of
Moveable Type, a foundry's own GitHub repo, etc.) go here. There's no
`METADATA.pb` for these, so a little bit of hand-entry replaces it.

## Layout

One folder per family, folder name doesn't matter (it's not used as the
slug — `family` in `meta.json` is). Inside:

```
manual-fonts/
  fraunces-text-velvetyne/
    meta.json              <- required
    OFL.txt                <- required: the license text, any .txt filename works
    FrauncesText-Regular.ttf
    FrauncesText-Bold.ttf
    FrauncesText-Italic.ttf
```

`meta.json`:

```json
{
  "family": "Fraunces Text",
  "license": "OFL-1.1",
  "source": "Velvetyne",
  "category": "serif"
}
```

- `family` — the display name. This also determines the slug/output folder
  (`fraunces-text`), so get it right.
- `license` — must be one of: `OFL-1.1`, `Apache-2.0`, `MIT`, `UFL-1.0`,
  `CC0-1.0`. Anything else is skipped rather than guessed at — if you're
  adding a font under a license not on this list, that's a deliberate edit
  to `MANUAL_LICENSE_ALLOWLIST` in `scripts/build.py`, not something to
  work around per-family.
- `source` — free text, shown in the manifest (e.g. `"Velvetyne"`). Also
  used to disambiguate if a family name collides with something already in
  the repo (you'd get `fraunces-text-velvetyne` as the output slug instead).
- `category` — optional; one of `serif`, `sans-serif`, `display`,
  `handwriting`, `monospace`, or omit it.

Drop in as many `.ttf`/`.otf` files as the family has (each style/weight as
its own physical file — this pipeline doesn't require them to be variable
fonts). Weight, italic, and variable-axis ranges are all read automatically
from each font's own OpenType tables. Script/language coverage (which
subset files get generated) is auto-detected the same way Google's own
tooling detects it for google/fonts — you don't need to declare subsets.

The `.example-your-family-slug-goes-here/` folder here is a template only
(the leading `.` makes the pipeline skip it) — copy it, rename it, and swap
in a real `meta.json` (drop the `.example` suffix) plus real font + license
files.
