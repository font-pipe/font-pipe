#!/usr/bin/env python3
"""
Font ingest pipeline: google/fonts (+ manually-added FOSS fonts) -> woff2 + manifest.json

Reads two kinds of sources:
  1. GOOGLE_FONTS_SRC: a sparse checkout of github.com/google/fonts (see workflow),
     containing only the families listed in `google-fonts-families.txt`.
     Ground truth for name/license/subsets/weights comes from each family's
     METADATA.pb, since Google's own CI already validated it against the fonts.
  2. MANUAL_FONTS_DIR: a folder of hand-added FOSS fonts from other sources
     (Velvetyne, League of Moveable Type, etc.), one subfolder per family, each
     containing a `meta.json`, a license .txt file, and one or more font files.
     There's no METADATA.pb here, so metadata is derived from the font's own
     OpenType tables plus the hand-written meta.json (for license/source/category,
     which can't be inferred).

Output: OUTPUT_DIR/<slug>/*.woff2 + license file, and OUTPUT_DIR/manifest.json.

See README.md for the on-disk conventions this script assumes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTFont
import gfsubsets

try:
    from gfmetadata import fonts_public_pb2
    from google.protobuf import text_format
except ImportError:
    fonts_public_pb2 = None
    text_format = None


# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

# google/fonts top-level directory -> our license id + the license filename
# that ships inside every family folder in that directory. This is ground
# truth (see writeup Phase 1) and is cross-checked against METADATA.pb's
# `license` field as a sanity check, not the other way around.
GOOGLE_DIR_LICENSE = {
    "ofl": ("OFL-1.1", "OFL.txt"),
    "apache": ("Apache-2.0", "LICENSE.txt"),
    "ufl": ("UFL-1.0", "UFL.txt"),
}

# METADATA.pb's `license` field uses these short codes.
METADATA_LICENSE_CODES = {
    "OFL-1.1": "OFL",
    "Apache-2.0": "APACHE2",
    "UFL-1.0": "UFL",
}

# Allowlist for manually-added (non-Google) fonts. Anything not in this list
# gets skipped, not guessed at, per the writeup's Phase 1 rule.
MANUAL_LICENSE_ALLOWLIST = {"OFL-1.1", "Apache-2.0", "MIT", "UFL-1.0", "CC0-1.0"}

# Subsets we don't ship as files. "menu" is Google's own pseudo-subset for
# rendering a family's name in a font picker (just the glyphs of the family
# name) -- it's not a real character-set subset useful for document text,
# and gfsubsets doesn't even know about it (it's METADATA-only), so there's
# nothing to subset-detect for non-Google fonts either. Skipped everywhere.
SKIP_SUBSETS = {"menu"}

# usWeightClass fallback lookup for manual/non-METADATA fonts, used only to
# cross-check (and log disagreements with) the subfamily name -- see Phase 2b.
WEIGHT_NAME_TO_CLASS = {
    "thin": 100, "hairline": 100,
    "extralight": 200, "ultralight": 200,
    "light": 300,
    "regular": 400, "normal": 400, "book": 400,
    "medium": 500,
    "semibold": 600, "demibold": 600,
    "bold": 700,
    "extrabold": 800, "ultrabold": 800,
    "black": 900, "heavy": 900,
}

# METADATA.pb category enum -> manifest category slug.
CATEGORY_SLUGS = {
    "SANS_SERIF": "sans-serif",
    "SERIF": "serif",
    "DISPLAY": "display",
    "HANDWRITING": "handwriting",
    "MONOSPACE": "monospace",
}

SUBSET_DETECT_MIN_PCT = 50
SUBSET_DETECT_EXT_MIN_PCT = 10


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def unicode_range_string(codepoints) -> str:
    cps = sorted(codepoints)
    if not cps:
        return ""
    ranges, start, prev = [], cps[0], cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        ranges.append((start, prev))
        start = prev = cp
    ranges.append((start, prev))
    return ", ".join(
        f"U+{a:04X}" if a == b else f"U+{a:04X}-{b:04X}" for a, b in ranges
    )


def safe_codepoints_in_subset(subset: str) -> set[int]:
    """Equivalent to gfsubsets.CodepointsInSubset(subset) (i.e. with the
    "-ext includes base, most subsets include latin" merging), but safe.

    The installed gfsubsets (as of this writing) has a real bug: its
    CodepointsInSubset(subset, unique_glyphs=False) returns a *reference* to
    its own internal cache and then does `cps |= other_cps` on it -- an
    in-place mutation of shared, memoized state. Call it enough times (e.g.
    once per family in a long-running batch job like this one) and the
    "cyrillic" entry silently absorbs "latin" forever, "cyrillic-ext"
    absorbs both, etc. Every subsequent detection or subsetting call in the
    same process then silently sees inflated, wrong codepoint sets.
    We replicate its (correct) merge logic here using only the
    `unique_glyphs=True` path, which returns the raw per-subset set, and
    copy it with `set(...)` before unioning so we never write back into
    gfsubsets' cache.
    """
    cps = set(gfsubsets.CodepointsInSubset(subset, unique_glyphs=True))
    if subset != "latin-ext" and subset.endswith("-ext"):
        cps |= set(gfsubsets.CodepointsInSubset(subset[:-4], unique_glyphs=True))
    if subset not in ("khmer", "latin"):
        cps |= set(gfsubsets.CodepointsInSubset("latin", unique_glyphs=True))
    return cps


def weight_token(weight_str: str) -> str:
    """'200 700' -> '200-700', '400' -> '400' -- used only in filenames."""
    return weight_str.replace(" ", "-")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


@dataclass
class FontSource:
    """One physical font file to process, with metadata already resolved."""
    family: str
    slug: str
    license: str
    license_file: Path
    source: str  # "google/fonts" or a manual source name
    category: str | None
    file_path: Path
    style: str  # "normal" | "italic"
    weight: str  # "400" or "200 700"
    stretch: str | None  # "75% 100%" or None
    subsets: list[str]  # which subsets to actually detect/emit for this file
    subsets_are_authoritative: bool  # True = trust `subsets` as-is (METADATA); False = re-detect per-file


@dataclass
class ManifestFile:
    path: str
    weight: str
    style: str
    unicodeRange: str
    stretch: str | None = None


@dataclass
class ManifestFamily:
    id: str
    family: str
    license: str
    source: str
    category: str | None
    subsets: list[str] = field(default_factory=list)
    subsetLabels: list[str] = field(default_factory=list)
    files: list[ManifestFile] = field(default_factory=list)


# --------------------------------------------------------------------------
# Phase 2a/2b: metadata extraction
# --------------------------------------------------------------------------

def read_fvar_axes(path: Path) -> dict[str, tuple[float, float]]:
    font = TTFont(str(path), lazy=True)
    if "fvar" not in font:
        return {}
    return {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}


def weight_and_stretch_from_axes(
    axes: dict[str, tuple[float, float]], fallback_weight: int
) -> tuple[str, str | None]:
    if "wght" in axes:
        lo, hi = axes["wght"]
        weight = f"{int(lo)} {int(hi)}" if lo != hi else str(int(lo))
    else:
        weight = str(fallback_weight)
    stretch = None
    if "wdth" in axes:
        lo, hi = axes["wdth"]
        stretch = f"{int(lo)}% {int(hi)}%" if lo != hi else f"{int(lo)}%"
    return weight, stretch


def discover_google_families(google_fonts_src: Path):
    """Yields (family_dir, license_dir_name) for every family folder present
    in the sparse checkout (whatever `git sparse-checkout set` pulled)."""
    for license_dir in ("ofl", "apache", "ufl"):
        base = google_fonts_src / license_dir
        if not base.is_dir():
            continue
        for family_dir in sorted(base.iterdir()):
            if family_dir.is_dir() and (family_dir / "METADATA.pb").exists():
                yield family_dir, license_dir
            elif family_dir.is_dir():
                log(f"WARNING: {family_dir} has no METADATA.pb, skipping "
                    f"(google/fonts families should always have one)")


def load_google_family(family_dir: Path, license_dir: str, used_slugs: dict) -> list[FontSource] | None:
    msg = fonts_public_pb2.FamilyProto()
    text_format.Merge((family_dir / "METADATA.pb").read_text(), msg)

    expected_license, license_filename = GOOGLE_DIR_LICENSE[license_dir]
    if msg.license and msg.license != METADATA_LICENSE_CODES.get(expected_license):
        log(f"WARNING: {family_dir}: METADATA license '{msg.license}' doesn't "
            f"match directory '{license_dir}' -- trusting the directory.")

    license_file = family_dir / license_filename
    if not license_file.exists():
        log(f"WARNING: {family_dir}: expected license file {license_filename} "
            f"missing, skipping family.")
        return None

    slug = resolve_slug(slugify(msg.name), msg.name, "google/fonts", used_slugs)
    category = CATEGORY_SLUGS.get(msg.category[0]) if msg.category else None
    subsets = [s for s in msg.subsets if s not in SKIP_SUBSETS]

    sources = []
    for f in msg.fonts:
        file_path = family_dir / f.filename
        if not file_path.exists():
            log(f"WARNING: {family_dir}: METADATA references missing file "
                f"{f.filename}, skipping that entry.")
            continue
        axes = read_fvar_axes(file_path)
        weight, stretch = weight_and_stretch_from_axes(axes, f.weight)
        sources.append(FontSource(
            family=msg.name, slug=slug, license=expected_license,
            license_file=license_file, source="google/fonts", category=category,
            file_path=file_path, style=f.style or "normal", weight=weight,
            stretch=stretch, subsets=subsets, subsets_are_authoritative=True,
        ))
    return sources


# --------------------------------------------------------------------------
# Manual (non-Google) fonts: Phase 2b + 2c
# --------------------------------------------------------------------------

def opentype_style_weight(path: Path) -> tuple[str, int]:
    font = TTFont(str(path), lazy=True)
    name_table, os2, head = font["name"], font.get("OS/2"), font["head"]
    subfamily = name_table.getDebugName(17) or name_table.getDebugName(2) or "Regular"
    weight = os2.usWeightClass if os2 else 400
    is_italic = bool(os2.fsSelection & 0x01) if os2 else bool(head.macStyle & 0x02)

    # Cross-check against the subfamily name (writeup: some foundries leave
    # usWeightClass at a default while naming the style "SemiBold" etc).
    norm = re.sub(r"[^a-z]", "", subfamily.lower()).replace("italic", "").replace("oblique", "")
    named_weight = WEIGHT_NAME_TO_CLASS.get(norm)
    if named_weight is not None and named_weight != weight:
        log(f"WARNING: {path}: usWeightClass={weight} but subfamily name "
            f"'{subfamily}' implies {named_weight} -- using usWeightClass, "
            f"please verify manually.")

    return ("italic" if is_italic else "normal"), weight


def discover_manual_families(manual_fonts_dir: Path):
    if not manual_fonts_dir.is_dir():
        return
    for family_dir in sorted(manual_fonts_dir.iterdir()):
        if not family_dir.is_dir() or family_dir.name.startswith("."):
            continue
        yield family_dir


def load_manual_family(family_dir: Path, used_slugs: dict) -> list[FontSource] | None:
    meta_path = family_dir / "meta.json"
    if not meta_path.exists():
        log(f"WARNING: {family_dir}: no meta.json, skipping.")
        return None
    meta = json.loads(meta_path.read_text())

    family_name = meta.get("family")
    license_id = meta.get("license")
    source = meta.get("source", "manual")
    category = meta.get("category")
    if not family_name or not license_id:
        log(f"WARNING: {family_dir}: meta.json missing 'family' or 'license', skipping.")
        return None
    if license_id not in MANUAL_LICENSE_ALLOWLIST:
        log(f"WARNING: {family_dir}: license '{license_id}' not in allowlist "
            f"{sorted(MANUAL_LICENSE_ALLOWLIST)}, skipping. Add it to the "
            f"allowlist deliberately if this is a mistake.")
        return None

    license_candidates = [p for p in family_dir.glob("*.txt")]
    if not license_candidates:
        log(f"WARNING: {family_dir}: no license .txt file found, skipping.")
        return None
    license_file = license_candidates[0]

    font_files = sorted(list(family_dir.glob("*.ttf")) + list(family_dir.glob("*.otf")))
    if not font_files:
        log(f"WARNING: {family_dir}: no .ttf/.otf files found, skipping.")
        return None

    slug = resolve_slug(slugify(family_name), family_name, source, used_slugs)

    sources = []
    for file_path in font_files:
        style, weight_class = opentype_style_weight(file_path)
        axes = read_fvar_axes(file_path)
        weight, stretch = weight_and_stretch_from_axes(axes, weight_class)
        # No METADATA subsets list for manual fonts -> detect per-file (2c).
        detected = gfsubsets.SubsetsInFont(
            str(file_path), SUBSET_DETECT_MIN_PCT, SUBSET_DETECT_EXT_MIN_PCT
        )
        subsets = [name for name, _, _ in detected if name not in SKIP_SUBSETS]
        sources.append(FontSource(
            family=family_name, slug=slug, license=license_id,
            license_file=license_file, source=source, category=category,
            file_path=file_path, style=style, weight=weight, stretch=stretch,
            subsets=subsets, subsets_are_authoritative=False,
        ))
    return sources


def resolve_slug(base_slug: str, family_name: str, source: str, used_slugs: dict) -> str:
    """Collision handling per writeup Phase 4: suffix with source name if two
    different foundries reuse a family name."""
    key = (family_name, source)
    if base_slug not in used_slugs:
        used_slugs[base_slug] = key
        return base_slug
    if used_slugs[base_slug] == key:
        return base_slug  # same family re-seen (shouldn't happen, but harmless)
    suffixed = f"{base_slug}-{slugify(source)}"
    log(f"NOTE: slug '{base_slug}' already used by {used_slugs[base_slug]}, "
        f"using '{suffixed}' for {key}.")
    used_slugs[suffixed] = key
    return suffixed


# --------------------------------------------------------------------------
# Phase 3: subset + convert to woff2
# --------------------------------------------------------------------------

def subset_to_woff2(src: FontSource, subset: str, out_path: Path) -> str | None:
    """Runs pyftsubset for one (font file, subset) pair. Returns the
    unicode-range string used, or None if the font has no glyphs in that
    subset (can legitimately happen at low detection thresholds)."""
    font_cps = gfsubsets.CodepointsInFont(str(src.file_path))
    subset_cps = safe_codepoints_in_subset(subset)
    cps = font_cps & subset_cps
    if not cps:
        return None
    rng = unicode_range_string(cps)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "pyftsubset", str(src.file_path),
        f"--unicodes={rng}",
        "--layout-features=*",
        "--flavor=woff2",
        f"--output-file={out_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"ERROR: pyftsubset failed for {src.file_path} [{subset}]: "
            f"{result.stderr.strip()}")
        return None
    return rng


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def process_family(sources: list[FontSource], output_dir: Path,
                    families: dict[str, ManifestFamily]) -> None:
    if not sources:
        return
    first = sources[0]
    slug = first.slug
    family_out = output_dir / slug

    if slug not in families:
        families[slug] = ManifestFamily(
            id=slug, family=first.family, license=first.license,
            source=first.source, category=first.category,
        )
    manifest_family = families[slug]

    # Copy the license file once per family.
    license_dest = family_out / first.license_file.name
    if not license_dest.exists():
        family_out.mkdir(parents=True, exist_ok=True)
        license_dest.write_bytes(first.license_file.read_bytes())

    all_subsets_seen = set(manifest_family.subsets)

    for src in sources:
        for subset in src.subsets:
            out_name = f"{slug}-{src.style}-{weight_token(src.weight)}-{subset}.woff2"
            out_path = family_out / out_name
            rng = subset_to_woff2(src, subset, out_path)
            if rng is None:
                log(f"  (skipped {out_name}: no glyphs in this subset after all)")
                continue
            manifest_family.files.append(ManifestFile(
                path=f"{slug}/{out_name}", weight=src.weight, style=src.style,
                unicodeRange=rng, stretch=src.stretch,
            ))
            all_subsets_seen.add(subset)
            print(f"  wrote {out_name} ({out_path.stat().st_size:,} bytes)")

    manifest_family.subsets = sorted(all_subsets_seen)
    manifest_family.subsetLabels = [subset_label(s) for s in manifest_family.subsets]


def subset_label(subset: str) -> str:
    return subset.replace("-ext", " Extended").replace("-", " ").title().replace(
        " Extended", " Extended"
    ).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--google-fonts-src", type=Path, default=Path("google-fonts-src"))
    ap.add_argument("--manual-fonts-dir", type=Path, default=Path("manual-fonts"))
    ap.add_argument("--output-dir", type=Path, default=Path("output"))
    args = ap.parse_args()

    if fonts_public_pb2 is None:
        log("ERROR: gfmetadata / protobuf not installed.")
        return 1

    used_slugs: dict = {}
    families: dict[str, ManifestFamily] = {}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    google_count = 0
    for family_dir, license_dir in discover_google_families(args.google_fonts_src):
        print(f"Processing (google/fonts): {family_dir}")
        sources = load_google_family(family_dir, license_dir, used_slugs)
        if sources:
            process_family(sources, args.output_dir, families)
            google_count += 1

    manual_count = 0
    for family_dir in discover_manual_families(args.manual_fonts_dir):
        print(f"Processing (manual): {family_dir}")
        sources = load_manual_family(family_dir, used_slugs)
        if sources:
            process_family(sources, args.output_dir, families)
            manual_count += 1

    manifest = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"),
        "families": [
            {
                "id": fam.id, "family": fam.family, "license": fam.license,
                "source": fam.source, "category": fam.category,
                "subsets": fam.subsets, "subsetLabels": fam.subsetLabels,
                "files": [vars(f) for f in fam.files],
            }
            for fam in sorted(families.values(), key=lambda f: f.id)
        ],
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nDone. {google_count} google/fonts families, {manual_count} manual "
          f"families, {len(manifest['families'])} total in manifest.")
    if not manifest["families"]:
        log("ERROR: manifest is empty -- nothing was processed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
