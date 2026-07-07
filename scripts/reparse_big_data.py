#!/usr/bin/env python
"""
reparse_big_data.py — recreate data.2020.big_data.txt with the CURRENT prosodic.

The 2020 dataset is one row per verse/prose line: prosodic-v1 metrical
annotations (meter, per-constraint violations, parse counts) plus literary
metadata (author, year, corpus, genre, ...). This script keeps the metadata and
the source `prosodic_line`, re-parses every line with the current prosodic
parser, and writes a new file with the prosodic columns recomputed.

Because the parser changed (vectorized, exhaustive, harmonic bounding) and the
constraint set was renamed/rewritten between v1 and v3, the recomputed columns
use CURRENT constraint names. To stay comparable with 2020 we use exactly the
five constraints that study used (see METER_KWARGS), which are all still
prosodic defaults today: w_peak, w_stress, s_unstress, unres_across,
unres_within. (Today's default adds a 6th, foot_size, deliberately dropped here
so bounding / num_parses / best-parse match the 2020 methodology.) This is still
a genuine reparse, so row counts, best scansions, and violation totals will
differ from 2020 — that is the point.

Each violation count also gets a `_per10syll` normalized column: bestparse
metrics ÷ num_sylls × 10 (length-normalized); allparse SUMS ÷ num_parses ÷
num_sylls × 10 (mean violations per parse per 10 syllables — normalized for both
line length and the number of surviving parses).

    Old (v1) column            -> New (v3) equivalent
    ------------------------------------------------------------------
    meter                      -> meter                 (best parse, w/s)
    num_parses                 -> num_parses            (# unbounded parses)
    num_sylls / num_words      -> num_sylls / num_words
    num_monosylls              -> num_monosylls
    num_viols                  -> num_viols_bestparse
    score_viols                -> score_bestparse       (weighted)
    viol_* / mviol_*_bestparse -> mviol_<constraint>_bestparse
    mviol_*_allparse_sum       -> mviol_<constraint>_allparse_sum
    prosodic_stress            -> prosodic_stress        (P/U; v1 also had S)
    prosodic_weight            -> prosodic_weight        (H/L)
    prosodic_sonority          -> (dropped; not exposed by current prosodic)
    parse                      -> (dropped; v1 "|"-delimited string not reproduced)

Usage:
    python scripts/reparse_big_data.py                    # full run (resumable)
    python scripts/reparse_big_data.py --limit 5000       # smoke test (first N rows)
    python scripts/reparse_big_data.py --chunk 4000       # rows per chunk/part
    python scripts/reparse_big_data.py --restart          # ignore existing parts
    python scripts/reparse_big_data.py --concat-only      # just assemble parts -> outputs

Resumable: each chunk is written to its own parquet part under PARTS_DIR; a
re-run skips completed parts and parses only what's missing. The final step
concatenates the parts into OUT_TSV (+ OUT_PARQUET). Kill it any time; re-run to
continue. Parts are keyed by chunk index, so keep --chunk fixed across resumes.

NOTE: the first run phonemizes any words not yet in prosodic's espeak cache
(~/prosodic_data/data/en_cache.tsv), which is slow; later runs are cache-warm.
"""

import argparse
import gc
import os
import sys
import time

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration (edit paths here, or override the common ones on the CLI).
# ---------------------------------------------------------------------------

# The current prosodic lives in a working copy, not the installed package —
# `by='line'` and the .bounded/.scansions fixes are not released yet. Point at
# the dev checkout so this picks them up (the 2020 notebook does the same).
# Once these land in a released prosodic, delete this block and just `import`.
PROSODIC_DEV = os.path.expanduser("~/github/prosodic")

DATA_DIR = os.path.expanduser("~/Dropbox/Prof/Articles/Antimetricality/data")
IN_PATH = os.path.join(DATA_DIR, "data.2020.big_data.txt")
OUT_TSV = os.path.join(DATA_DIR, "data.2026.reparse.big_data.txt")
OUT_PARQUET = os.path.join(DATA_DIR, "data.2026.reparse.big_data.parquet")
PARTS_DIR = os.path.join(DATA_DIR, "_reparse_parts")  # per-chunk parquet parts

TEXT_COL = "prosodic_line"     # the line to re-parse
CHUNK_DEFAULT = 4000           # rows per chunk / part

# Metadata + identity columns carried through verbatim. Everything NOT listed
# here is prosodic-derived and gets recomputed (old prosodic columns are dropped).
PASSTHROUGH_COLS = [
    "line_id", "id", "prosodic_line",
    "author", "author_dob", "corpus", "genre", "medium", "metagenre",
    "num_lines_parsed", "title", "y30_or_year", "year", "year_author_is_30",
]

# Meter configuration for the reparse. The 2020 study used these five
# constraints (all still prosodic defaults today). foot_size — a 6th current
# default — is intentionally DROPPED so bounding / num_parses / best-parse match
# the 2020 methodology. Drop the "constraints" key to use today's full default 6.
METER_KWARGS = {
    "constraints": (
        "w_peak",        # 2020 strength_w_is_p  (weak position on a stress peak)
        "w_stress",      # 2020 stress_w_is_p    (stress in weak position)
        "s_unstress",    # 2020 stress_s_is_u    (unstress in strong position)
        "unres_across",  # 2020 footmin-f-resolution
        "unres_within",  # 2020 footmin-w-resolution
    ),
}


# Iambic pentameter = weak-strong × 5. Two encodings: `by='line'` reports the
# scansion in '+'/'-' (strong/weak); the human-facing `meter` column is 's'/'w'.
IAMBIC_PENTAMETER_SW = "ws" * 5   # "wswswswsws"  (the meter column form)
IAMBIC_PENTAMETER_PM = "-+" * 5   # "-+-+-+-+-+"  (get_parses_df by='line' form)


class Misaligned(Exception):
    """Raised when a batched chunk's prosodic lines don't map 1:1 to inputs."""


# ---------------------------------------------------------------------------
# prosodic import (dev checkout first) + capability check
# ---------------------------------------------------------------------------

def import_prosodic():
    if PROSODIC_DEV and os.path.isdir(PROSODIC_DEV):
        sys.path.insert(0, PROSODIC_DEV)
    import prosodic  # noqa: E402
    from prosodic.texts.texts import TextModel  # noqa: E402
    # Fail loudly if we picked up a prosodic without by='line'.
    probe = TextModel("the cat sat on the mat")
    try:
        probe.get_parses_df(mode="best", by="line")
    except TypeError as e:
        raise SystemExit(
            "The prosodic on sys.path does not support get_parses_df(by='line').\n"
            f"  using: {prosodic.__file__}\n"
            f"Point PROSODIC_DEV at the working copy that has the by='line' change "
            f"(currently {PROSODIC_DEV!r})."
        ) from e
    constraints = list(probe.get_meter(**METER_KWARGS).constraints.keys())
    return prosodic, TextModel, constraints


# ---------------------------------------------------------------------------
# Per-line feature extraction from a parsed TextModel (no entity construction)
# ---------------------------------------------------------------------------

def _syll_line_features(t):
    """Per-line strings/counts from the canonical (form_idx==0) syllables.

    Returns {line_num: dict(num_words, num_monosylls, prosodic_stress,
    prosodic_weight)}. Available whether or not the line parsed.
    """
    sdf = t._syll_df
    canon = sdf[sdf["form_idx"] == 0]
    if "is_punc" in canon.columns:
        canon = canon[~canon["is_punc"].astype(bool)]
    out = {}
    for line_num, g in canon.groupby("line_num", sort=True):
        g = g.sort_values(["word_num", "syll_idx"])
        stressed = g["is_stressed"].astype(bool).tolist()
        heavy = g["is_heavy"].astype(bool).tolist()
        # words with exactly one canonical syllable
        syll_per_word = g.groupby("word_num").size()
        out[int(line_num)] = {
            # canonical (form_idx==0) syllable count — independent of the parse.
            # 2020 data is 100% 10-syll by construction, so this measures v1->v3
            # syllabifier drift (how many lines v3 now counts as != 10).
            "num_sylls_canonical": int(len(g)),
            "num_words": int(g["word_num"].nunique()),
            "num_monosylls": int((syll_per_word == 1).sum()),
            "prosodic_stress": "".join("P" if s else "U" for s in stressed),
            "prosodic_weight": "".join("H" if h else "L" for h in heavy),
        }
    return out


def _aggregate(t, n_expected, constraints):
    """Aggregate a parsed TextModel into n_expected per-line rows.

    Maps the k-th smallest prosodic line_num to input position k (0-indexed),
    so it is agnostic to whether prosodic numbers lines from 0 or 1. Raises
    Misaligned if the number of tokenized lines != n_expected (a line split or
    dropped), so the caller can fall back to per-line parsing.

    Returns a DataFrame with RangeIndex 0..n_expected-1 (input order).
    """
    sdf = t._syll_df
    present = sorted(int(x) for x in sdf["line_num"].unique())
    if len(present) != n_expected:
        raise Misaligned(f"{len(present)} tokenized lines != {n_expected} inputs")
    pos_of = {ln: i for i, ln in enumerate(present)}  # line_num -> input position

    syll_feats = _syll_line_features(t)

    # per-parse rows (unbounded only) collapsed to one row per parse
    pdf = t.get_parses_df(mode="unbounded", by="line", **METER_KWARGS)

    star = {c: f"*{c}" for c in constraints}  # constraint -> violation column
    rows = [None] * n_expected

    if len(pdf):
        gb = pdf.groupby("line_num", sort=True)
        for line_num, g in gb:
            line_num = int(line_num)
            if line_num not in pos_of:
                # tokenized but not in our position map (shouldn't happen given
                # the length check) — skip defensively
                continue
            best = g[g["is_best"]]
            best = best.iloc[0] if len(best) else g.sort_values("parse_score").iloc[0]
            ns = int(best["num_sylls"])
            npar = int(len(g))
            # bestparse metrics normalize per 10 syllables; allparse SUMS are
            # totals over `npar` parses, so normalize per parse AND per 10 sylls
            # (= mean violations per parse per 10 syllables).
            per10 = lambda v: (v / ns * 10) if ns else pd.NA
            per_parse10 = lambda v: (v / npar / ns * 10) if (ns and npar) else pd.NA
            nv_best = int(best["num_viols"])
            nv_all = int(g["num_viols"].sum())
            score_best = float(best["parse_score"])
            meter_sw = str(best["meter"]).translate(str.maketrans("+-", "sw"))
            rec = {
                "is_parsed": True,
                "meter": meter_sw,
                "is_iambic_pentameter_best": meter_sw == IAMBIC_PENTAMETER_SW,
                "is_iambic_pentameter_any": bool((g["meter"] == IAMBIC_PENTAMETER_PM).any()),
                "num_sylls": ns,
                "num_parses": npar,
                "num_viols_bestparse": nv_best,
                "num_viols_bestparse_per10syll": per10(nv_best),
                "num_viols_allparse_sum": nv_all,
                "num_viols_allparse_sum_per10syll": per_parse10(nv_all),
                "score_bestparse": score_best,
                "score_bestparse_per10syll": per10(score_best),
            }
            for c, col in star.items():
                bp = int(best[col]) if col in g.columns else 0
                ap = int(g[col].sum()) if col in g.columns else 0
                rec[f"mviol_{c}_bestparse"] = bp
                rec[f"mviol_{c}_bestparse_per10syll"] = per10(bp)
                rec[f"mviol_{c}_allparse_sum"] = ap
                rec[f"mviol_{c}_allparse_sum_per10syll"] = per_parse10(ap)
            rows[pos_of[line_num]] = rec

    # fill unparsed lines (too short/long) + attach syllable features to all
    for line_num, pos in pos_of.items():
        feats = syll_feats.get(line_num, {})
        if rows[pos] is None:
            rec = {
                "is_parsed": False,
                "meter": pd.NA,
                "is_iambic_pentameter_best": False,
                "is_iambic_pentameter_any": False,
                "num_sylls": pd.NA, "num_parses": 0,
                "num_viols_bestparse": pd.NA, "num_viols_bestparse_per10syll": pd.NA,
                "num_viols_allparse_sum": pd.NA, "num_viols_allparse_sum_per10syll": pd.NA,
                "score_bestparse": pd.NA, "score_bestparse_per10syll": pd.NA,
            }
            for c in constraints:
                rec[f"mviol_{c}_bestparse"] = pd.NA
                rec[f"mviol_{c}_bestparse_per10syll"] = pd.NA
                rec[f"mviol_{c}_allparse_sum"] = pd.NA
                rec[f"mviol_{c}_allparse_sum_per10syll"] = pd.NA
            rows[pos] = rec
        rows[pos].update(feats)

    return pd.DataFrame(rows, index=range(n_expected))


def _parse_batch(TextModel, line_texts, constraints):
    """Batch-parse all lines as one TextModel; raise Misaligned on mismatch."""
    t = TextModel("\n".join(line_texts))
    try:
        return _aggregate(t, len(line_texts), constraints)
    finally:
        try:
            t.cleanup()
        except Exception:
            pass
        del t
        gc.collect()


def _parse_perline(TextModel, line_texts, constraints):
    """Robust fallback: parse each line as its own TextModel (slow)."""
    frames = []
    for txt in line_texts:
        t = TextModel(txt)
        try:
            frames.append(_aggregate(t, 1, constraints))
        except Misaligned:
            # even a single line failed to tokenize to one line — emit a blank
            frames.append(_aggregate_blank(constraints))
        finally:
            try:
                t.cleanup()
            except Exception:
                pass
            del t
    df = pd.concat(frames, ignore_index=True)
    gc.collect()
    return df


def _aggregate_blank(constraints):
    rec = {
        "is_parsed": False, "meter": pd.NA,
        "is_iambic_pentameter_best": False, "is_iambic_pentameter_any": False,
        "num_sylls": pd.NA, "num_sylls_canonical": pd.NA, "num_parses": 0,
        "num_viols_bestparse": pd.NA, "num_viols_bestparse_per10syll": pd.NA,
        "num_viols_allparse_sum": pd.NA, "num_viols_allparse_sum_per10syll": pd.NA,
        "score_bestparse": pd.NA, "score_bestparse_per10syll": pd.NA,
        "num_words": pd.NA, "num_monosylls": pd.NA,
        "prosodic_stress": pd.NA, "prosodic_weight": pd.NA,
    }
    for c in constraints:
        rec[f"mviol_{c}_bestparse"] = pd.NA
        rec[f"mviol_{c}_bestparse_per10syll"] = pd.NA
        rec[f"mviol_{c}_allparse_sum"] = pd.NA
        rec[f"mviol_{c}_allparse_sum_per10syll"] = pd.NA
    return pd.DataFrame([rec], index=[0])


def process_chunk(cdf, TextModel, constraints, offset=0):
    """Recompute prosodic columns for one input chunk (a DataFrame slice).

    `offset` is the number of input rows before this chunk, used to stamp a
    unique `orig_row` (0-based index into the 2020 file) — the source data's
    `line_id` is NOT unique, so this is the only reliable join key.

    Returns cdf's PASSTHROUGH columns + the recomputed columns, same row order.
    """
    keep = cdf[[c for c in PASSTHROUGH_COLS if c in cdf.columns]].reset_index(drop=True)
    keep.insert(0, "orig_row", list(range(offset, offset + len(cdf))))
    texts = cdf[TEXT_COL].fillna("").map(lambda s: str(s).strip()).reset_index(drop=True)
    nonblank_pos = texts[texts.str.len() > 0].index.tolist()

    recomputed = pd.DataFrame(index=range(len(cdf)))
    if nonblank_pos:
        nb_texts = [texts.iloc[i] for i in nonblank_pos]
        try:
            rc = _parse_batch(TextModel, nb_texts, constraints)
        except Misaligned:
            rc = _parse_perline(TextModel, nb_texts, constraints)
        rc.index = nonblank_pos  # map back to chunk row positions
        recomputed = recomputed.join(rc)

    return pd.concat([keep, recomputed], axis=1)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def part_path(idx):
    return os.path.join(PARTS_DIR, f"part_{idx:05d}.parquet")


def run(chunk_size, limit, restart, concat_only):
    os.makedirs(PARTS_DIR, exist_ok=True)

    if not concat_only:
        # import prosodic only when actually parsing — concat-only just reads
        # the parquet parts and needs no parser (nor the by='line' capability).
        prosodic, TextModel, constraints = import_prosodic()
        print(f"prosodic: {prosodic.__file__}")
        print(f"constraints ({len(constraints)}): {', '.join(constraints)}")
        reader = pd.read_csv(IN_PATH, sep="\t", low_memory=False, chunksize=chunk_size)
        seen = 0
        t0 = time.time()
        for idx, cdf in enumerate(reader):
            offset = seen  # rows before this chunk -> orig_row base
            if limit is not None and seen >= limit:
                break
            if limit is not None and seen + len(cdf) > limit:
                cdf = cdf.iloc[: limit - seen]
            seen += len(cdf)

            pp = part_path(idx)
            if os.path.exists(pp) and not restart:
                print(f"[{idx:05d}] skip (exists)  rows so far: {seen}")
                continue

            ct = time.time()
            out = process_chunk(cdf, TextModel, constraints, offset)
            out.to_parquet(pp, index=False)
            dt = time.time() - ct
            rate = len(cdf) / dt if dt else 0
            print(f"[{idx:05d}] {len(cdf):5d} rows in {dt:6.1f}s "
                  f"({rate:5.0f}/s)  total: {seen}  elapsed: {time.time()-t0:6.0f}s")

    # assemble parts -> final outputs
    parts = sorted(
        os.path.join(PARTS_DIR, f) for f in os.listdir(PARTS_DIR)
        if f.startswith("part_") and f.endswith(".parquet")
    )
    if not parts:
        print("no parts to assemble.")
        return
    print(f"concatenating {len(parts)} parts ...")
    full = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)

    # Parts are dtype-inferred per chunk, so a column can disagree across parts
    # and concat to a mixed-type `object` column pyarrow can't serialize — e.g.
    # `id` is int64 in an all-numeric-id chunk and str elsewhere. Normalize:
    # the recomputed flags -> nullable boolean, every remaining object column
    # (identifiers, text, meter/stress strings) -> nullable string. Numeric
    # columns (int64/float64) are left untouched.
    for c in ("is_parsed", "is_iambic_pentameter_best", "is_iambic_pentameter_any"):
        if c in full.columns:
            full[c] = full[c].astype("boolean")
    for c in full.select_dtypes(include="object").columns:
        full[c] = full[c].astype("string")

    # TSV first (the primary, always-serializable deliverable), then parquet.
    full.to_csv(OUT_TSV, sep="\t", index=False)
    full.to_parquet(OUT_PARQUET, index=False)
    print(f"wrote {len(full):,} rows:\n  {OUT_TSV}\n  {OUT_PARQUET}")
    n_parsed = int(full["is_parsed"].fillna(False).sum()) if "is_parsed" in full else 0
    print(f"parsed: {n_parsed:,} / {len(full):,} "
          f"({100*n_parsed/max(len(full),1):.1f}%)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunk", type=int, default=CHUNK_DEFAULT,
                    help=f"rows per chunk/part (default {CHUNK_DEFAULT}); keep fixed across resumes")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N input rows (smoke test)")
    ap.add_argument("--restart", action="store_true",
                    help="re-parse chunks even if their part file already exists")
    ap.add_argument("--concat-only", action="store_true",
                    help="skip parsing; just assemble existing parts into the outputs")
    args = ap.parse_args()
    run(args.chunk, args.limit, args.restart, args.concat_only)


if __name__ == "__main__":
    main()
