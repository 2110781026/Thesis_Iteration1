"""
correlate.py — Iteration 3 correlation and visualisation engine
===============================================================

Usage:
    python correlate.py --serial serial_YYYYMMDD_HHMMSS.csv \
                        --keylog keyboard_YYYYMMDD_HHMMSS_filtered.csv \
                        --raw    keyboard_YYYYMMDD_HHMMSS_raw.csv

Outputs written to current directory:
    correlated.csv       — one row per matched keypress with t_latency_ms
    stats.csv            — per-key descriptive statistics
    outliers.csv         — rows removed by 3-sigma rule (if any)
    crossval.csv         — cross-validation delta per press
    hist_latency.png     — latency distribution histogram (all keys overlaid)
    boxplot_per_key.png  — box plot per letter
    scatter_stability.png— latency over time (drift / stability check)
    overlay_pattern.png  — keystroke dynamics overlay (all cycles stacked)
    crossval.png         — actual Pico T0 vs pattern-expected T0
"""

import argparse
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ---------------------------------------------------------------------------
# Pattern definition — must match generator
# ---------------------------------------------------------------------------
PATTERN   = list("FHBURGENLAND")
PATTERN_LEN = len(PATTERN)

PALETTE = [
    "#4C72B0", "#DD8452", "#55A868", "#C44E52",
    "#8172B3", "#937860", "#DA8BC3", "#8C8C8C",
    "#CCB974", "#64B5CD", "#4C72B0", "#DD8452",
]
CHAR_COLOR = {c: PALETTE[i] for i, c in enumerate(PATTERN)}
FIGURE_DPI = 150

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def clean_keyname(series: pd.Series) -> pd.Series:
    """
    Strip all whitespace and quote characters from keyname values.
    The filtered CSV stores keynames as "F", "H" etc — pandas read_csv
    may preserve the surrounding quotes as literal characters depending
    on the quoting mode used when the C++ logger wrote the file.
    This function handles all variants: 'F', '"F"', ' F ', ' "F" '.
    """
    return (series
            .astype(str)
            .str.strip()
            .str.strip('"')
            .str.strip("'")
            .str.strip()
            .str.upper())


def load_serial(path: str) -> pd.DataFrame:
    """Load serial logger CSV. Comment lines starting with # are skipped."""
    df = pd.read_csv(
        path,
        comment="#",
        dtype={"seq": int, "cycle": int, "pos": int,
               "gpio": int, "t0_us": int},
        keep_default_na=False,
    )
    df["char"]  = clean_keyname(df["char"])
    df["t0_ns"] = df["t0_us"].astype(np.int64) * 1000
    return df.sort_values("seq").reset_index(drop=True)


def load_keylog(path: str) -> pd.DataFrame:
    """Load filtered key logger CSV (DOWN events only, debounced)."""
    df = pd.read_csv(
        path,
        dtype={"vkey": int, "scancode": int, "t1_ns": int},
        keep_default_na=False,
    )
    df["keyname"] = clean_keyname(df["keyname"])
    return df.reset_index(drop=True)


def load_raw(path: str) -> pd.DataFrame:
    """Load raw key logger CSV (all events including UP)."""
    df = pd.read_csv(path, keep_default_na=False)
    df["keyname"] = clean_keyname(df["keyname"])
    df["edge"]    = df["edge"].str.strip().str.upper()
    return df.reset_index(drop=True)

# ---------------------------------------------------------------------------
# Sequence alignment
#
# The serial logger and key logger are started independently. The key logger
# often captures a few events before the serial logger begins — or vice versa.
# This means the two sequences may be offset by N rows at the start.
#
# Strategy: scan the first SEARCH_WINDOW rows of the longer file to find the
# offset that produces zero mismatches on the first PATTERN_LEN rows.
# This is a sliding-window search over the start of the longer sequence.
# ---------------------------------------------------------------------------
SEARCH_WINDOW = 24   # search up to 2 full pattern cycles worth of offset


def align_sequences(serial: pd.DataFrame,
                    keylog: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Find and remove any leading rows in keylog or serial that cause the
    two sequences to be out of sync. Returns trimmed (serial, keylog).
    """
    s_chars = serial["char"].tolist()
    k_chars = keylog["keyname"].tolist()

    # Try trimming the start of keylog (most common case: keylog started first)
    best_offset = 0
    best_score  = -1
    for offset in range(SEARCH_WINDOW + 1):
        n = min(len(s_chars), len(k_chars) - offset)
        if n <= 0:
            break
        matches = sum(s == k for s, k in
                      zip(s_chars[:n], k_chars[offset:offset + n]))
        if matches > best_score:
            best_score  = matches
            best_offset = offset

    if best_offset > 0:
        print(f"[ALIGN] Trimmed {best_offset} leading row(s) from keylog "
              f"(keylog started before serial logger)")
        keylog = keylog.iloc[best_offset:].reset_index(drop=True)
        return serial, keylog

    # Try trimming the start of serial (less common: serial started first)
    best_offset = 0
    best_score  = -1
    for offset in range(1, SEARCH_WINDOW + 1):
        n = min(len(s_chars) - offset, len(k_chars))
        if n <= 0:
            break
        matches = sum(s == k for s, k in
                      zip(s_chars[offset:offset + n], k_chars[:n]))
        if matches > best_score:
            best_score  = matches
            best_offset = offset

    if best_offset > 0:
        print(f"[ALIGN] Trimmed {best_offset} leading row(s) from serial log "
              f"(serial logger started before key logger)")
        serial = serial.iloc[best_offset:].reset_index(drop=True)

    return serial, keylog

# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def correlate(serial: pd.DataFrame, keylog: pd.DataFrame) -> pd.DataFrame:
    n = min(len(serial), len(keylog))
    if len(serial) != len(keylog):
        print(f"[WARN] Row count mismatch after alignment: "
              f"serial={len(serial)}, keylog={len(keylog)}. "
              f"Using first {n} rows.")

    s = serial.iloc[:n].reset_index(drop=True)
    k = keylog.iloc[:n].reset_index(drop=True)

    mismatch_mask = s["char"] != k["keyname"]
    n_mismatch    = int(mismatch_mask.sum())

    if n_mismatch > 0:
        print(f"[WARN] {n_mismatch} character mismatches after alignment. "
              f"See correlation_errors.csv")
        errors = pd.DataFrame({
            "row":         mismatch_mask[mismatch_mask].index.tolist(),
            "serial_seq":  s.loc[mismatch_mask, "seq"].tolist(),
            "serial_char": s.loc[mismatch_mask, "char"].tolist(),
            "keylog_key":  k.loc[mismatch_mask, "keyname"].tolist(),
        })
        errors.to_csv("correlation_errors.csv", index=False)
    else:
        print(f"[OK]  All {n} rows matched cleanly — no character mismatches")

    corr = pd.DataFrame({
        "seq":          s["seq"],
        "cycle":        s["cycle"],
        "pos":          s["pos"],
        "char":         s["char"],
        "gpio":         s["gpio"],
        "t0_ns":        s["t0_ns"],
        "t1_ns":        k["t1_ns"].astype(np.int64),
        "t_latency_ns": k["t1_ns"].astype(np.int64) - s["t0_ns"],
        "valid":        ~mismatch_mask,
    })
    corr["t_latency_ms"] = corr["t_latency_ns"] / 1_000_000.0
    return corr

# ---------------------------------------------------------------------------
# Clock offset correction
# ---------------------------------------------------------------------------

def apply_clock_offset(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """
    The Pico clock origin (us since boot) and the Windows QPC origin
    (ns since system boot) are completely different. The raw t_latency_ns
    will be a large value reflecting this difference. We subtract the
    median latency of the first complete cycle as a fixed baseline offset,
    leaving only the per-press variation — which is what we are measuring.
    """
    first_cycle_id = df["cycle"].min()
    first_cycle    = df[df["cycle"] == first_cycle_id]
    offset_ns      = float(first_cycle["t_latency_ns"].median())

    df = df.copy()
    df["t_latency_ns_raw"] = df["t_latency_ns"]
    df["t_latency_ns"]     = df["t_latency_ns"] - int(offset_ns)
    df["t_latency_ms"]     = df["t_latency_ns"] / 1_000_000.0

    print(f"[INFO] Clock offset (median of first cycle): "
          f"{offset_ns/1e6:.3f} ms  —  subtracted from all latency values")
    return df, offset_ns

# ---------------------------------------------------------------------------
# Outlier removal
# ---------------------------------------------------------------------------

def remove_outliers(df: pd.DataFrame,
                    col: str   = "t_latency_ms",
                    sigma: float = 3.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-key 3-sigma outlier removal."""
    clean_parts, outlier_parts = [], []
    for char in PATTERN:
        grp = df[df["char"] == char]
        if len(grp) == 0:
            continue
        mean = grp[col].mean()
        std  = grp[col].std()
        if std == 0 or np.isnan(std):
            clean_parts.append(grp)
            continue
        mask = (grp[col] - mean).abs() <= sigma * std
        clean_parts.append(grp[mask])
        outlier_parts.append(grp[~mask])

    clean = (pd.concat(clean_parts)
               .sort_values("seq")
               .reset_index(drop=True))

    if outlier_parts:
        outliers = (pd.concat(outlier_parts)
                      .sort_values("seq")
                      .reset_index(drop=True))
        if len(outliers) > 0:
            print(f"[INFO] {len(outliers)} outlier(s) removed (>{sigma}σ per key) "
                  f"— see outliers.csv")
            outliers.to_csv("outliers.csv", index=False)
    else:
        outliers = pd.DataFrame()

    return clean, outliers

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for char in PATTERN:
        grp = df[df["char"] == char]["t_latency_ms"]
        if len(grp) == 0:
            continue
        rows.append({
            "char":      char,
            "count":     len(grp),
            "mean_ms":   round(grp.mean(),          4),
            "median_ms": round(grp.median(),         4),
            "std_ms":    round(grp.std(),            4),
            "min_ms":    round(grp.min(),            4),
            "max_ms":    round(grp.max(),            4),
            "p5_ms":     round(grp.quantile(0.05),  4),
            "p95_ms":    round(grp.quantile(0.95),  4),
        })
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------

def crossvalidate(serial: pd.DataFrame,
                  press_ms: int = 30,
                  interval_ms: int = 70) -> pd.DataFrame:
    """
    For each press, compute expected T0 from pattern schedule and compare
    to actual Pico T0. Delta near zero and stable = generator is precise.
    Growing delta = clock drift.
    """
    period_us = (press_ms + interval_ms) * 1000
    rows = []
    for cycle_id, grp in serial.groupby("cycle"):
        grp = grp.sort_values("pos")
        starts = grp[grp["pos"] == 0]["t0_us"].values
        if len(starts) == 0:
            continue
        t0_start = int(starts[0])
        for _, row in grp.iterrows():
            expected  = t0_start + int(row["pos"]) * period_us
            delta_us  = int(row["t0_us"]) - expected
            rows.append({
                "seq":            int(row["seq"]),
                "cycle":          cycle_id,
                "pos":            int(row["pos"]),
                "char":           row["char"],
                "t0_us":          int(row["t0_us"]),
                "t0_expected_us": expected,
                "delta_us":       delta_us,
            })
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def plot_histogram(df: pd.DataFrame, out: str = "hist_latency.png") -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    all_lat = df["t_latency_ms"]
    xmin = max(0, all_lat.min() - 2)
    xmax = all_lat.max() + 2
    bins = np.arange(xmin, xmax + 0.5, 0.5)

    for char in PATTERN:
        grp = df[df["char"] == char]["t_latency_ms"]
        if len(grp) == 0:
            continue
        ax.hist(grp, bins=bins, alpha=0.45, label=char,
                color=CHAR_COLOR[char], edgecolor="none")

    ax.set_xlabel("Latency (ms)", fontsize=12)
    ax.set_ylabel("Count", fontsize=12)
    ax.set_title("Latency distribution — all keys overlaid (0.5 ms bins)",
                 fontsize=13)
    ax.legend(title="Key", ncol=4, fontsize=9)
    ax.xaxis.set_minor_locator(ticker.MultipleLocator(0.5))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_boxplot_per_key(df: pd.DataFrame,
                         out: str = "boxplot_per_key.png") -> None:
    fig, ax = plt.subplots(figsize=(12, 5))
    data   = [df[df["char"] == c]["t_latency_ms"].values for c in PATTERN]
    colors = [CHAR_COLOR[c] for c in PATTERN]

    bp = ax.boxplot(data, patch_artist=True, notch=False,
                    medianprops=dict(color="black", linewidth=1.5),
                    whiskerprops=dict(linewidth=1),
                    capprops=dict(linewidth=1),
                    flierprops=dict(marker="o", markersize=3,
                                   alpha=0.4, linestyle="none"))
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_xticks(range(1, len(PATTERN) + 1))
    ax.set_xticklabels(PATTERN)
    ax.set_xlabel("Key", fontsize=12)
    ax.set_ylabel("Latency (ms)", fontsize=12)
    ax.set_title("Per-key latency distribution", fontsize=13)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_scatter_stability(df: pd.DataFrame,
                           out: str = "scatter_stability.png") -> None:
    fig, ax = plt.subplots(figsize=(12, 4))
    for char in PATTERN:
        grp = df[df["char"] == char]
        ax.scatter(grp["seq"], grp["t_latency_ms"],
                   s=4, alpha=0.4, color=CHAR_COLOR[char], label=char)

    ax.set_xlabel("Sequence number", fontsize=12)
    ax.set_ylabel("Latency (ms)", fontsize=12)
    ax.set_title("Latency stability over run", fontsize=13)
    ax.legend(title="Key", ncol=4, fontsize=8, markerscale=2)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_pattern_overlay(df: pd.DataFrame,
                         press_ms: int = 30,
                         interval_ms: int = 70,
                         max_cycles: int = 200,
                         out: str = "overlay_pattern.png") -> None:
    period_ms = press_ms + interval_ms
    cycles = sorted(df["cycle"].unique())
    if len(cycles) > max_cycles:
        cycles = cycles[:max_cycles]

    fig, ax = plt.subplots(figsize=(14, max(4, len(cycles) * 0.12)))

    for cy_idx, cycle_id in enumerate(cycles):
        grp = df[df["cycle"] == cycle_id].sort_values("pos")
        if len(grp) == 0:
            continue
        first_t1 = grp["t1_ns"].min()
        for _, row in grp.iterrows():
            x_actual = (row["t1_ns"] - first_t1) / 1_000_000.0
            ax.scatter(x_actual, cy_idx, s=6,
                       color=CHAR_COLOR.get(row["char"], "#888888"),
                       alpha=0.5, linewidths=0)

    for pos, char in enumerate(PATTERN):
        x_exp = pos * period_ms
        ax.axvline(x_exp, color=CHAR_COLOR[char],
                   linewidth=0.5, alpha=0.3, linestyle="--")
        ax.text(x_exp, -1.5, char, ha="center", va="top",
                fontsize=7, color=CHAR_COLOR[char])

    ax.set_xlabel("Time within pattern cycle (ms)", fontsize=11)
    ax.set_ylabel("Cycle index", fontsize=11)
    ax.set_title("Keystroke dynamics overlay — OS arrival times per cycle",
                 fontsize=12)
    ax.set_xlim(-10, len(PATTERN) * period_ms + 10)
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=0.15)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_crossval(cv: pd.DataFrame,
                  out: str = "crossval.png") -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6), sharex=True)

    for char in PATTERN:
        grp = cv[cv["char"] == char]
        if len(grp) == 0:
            continue
        ax1.scatter(grp["seq"], grp["delta_us"],
                    s=4, alpha=0.5,
                    color=CHAR_COLOR[char], label=char)

    ax1.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax1.set_ylabel("T0 delta (µs)\nactual − expected", fontsize=10)
    ax1.set_title("Cross-validation: actual Pico T0 vs pattern-expected T0",
                  fontsize=12)
    ax1.legend(title="Key", ncol=4, fontsize=7, markerscale=2)
    ax1.grid(alpha=0.2)

    cv_sorted = cv.sort_values("seq")
    rolling   = (cv_sorted["delta_us"]
                 .abs()
                 .rolling(window=12, center=True, min_periods=1)
                 .mean())
    ax2.plot(cv_sorted["seq"], rolling, color="#4C72B0", linewidth=1)
    ax2.set_xlabel("Sequence number", fontsize=10)
    ax2.set_ylabel("Rolling |delta| (µs)\n12-key window", fontsize=10)
    ax2.grid(alpha=0.2)

    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlate generator serial log with OS key logger output.")
    parser.add_argument("--serial",  required=True,
                        help="Serial logger CSV")
    parser.add_argument("--keylog",  required=True,
                        help="Filtered key logger CSV (*_filtered.csv)")
    parser.add_argument("--raw",     required=False, default=None,
                        help="Raw key logger CSV (*_raw.csv) — optional")
    parser.add_argument("--press-ms",    type=int, default=30)
    parser.add_argument("--interval-ms", type=int, default=70)
    parser.add_argument("--sigma",       type=float, default=3.0)
    parser.add_argument("--max-overlay-cycles", type=int, default=200)
    args = parser.parse_args()

    # ---- Load ---------------------------------------------------------------
    print(f"\n[IN]  Loading serial log  : {args.serial}")
    serial = load_serial(args.serial)
    print(f"      {len(serial)} generator events loaded")
    print(f"      First char: '{serial['char'].iloc[0]}'  "
          f"Last char: '{serial['char'].iloc[-1]}'")

    print(f"[IN]  Loading key log     : {args.keylog}")
    keylog = load_keylog(args.keylog)
    print(f"      {len(keylog)} OS key events loaded")
    print(f"      First key: '{keylog['keyname'].iloc[0]}'  "
          f"Last key: '{keylog['keyname'].iloc[-1]}'")

    # ---- Align sequences ----------------------------------------------------
    print("[...] Aligning sequences...")
    serial, keylog = align_sequences(serial, keylog)
    print(f"      After alignment: serial={len(serial)}, keylog={len(keylog)}")

    # ---- Correlate ----------------------------------------------------------
    print("[...] Correlating...")
    corr = correlate(serial, keylog)
    valid = corr[corr["valid"]].copy()
    print(f"      {len(valid)} / {len(corr)} events correlated successfully")

    if len(valid) == 0:
        print("\n[ERROR] No valid correlated events. Possible causes:")
        print("  1. The keyname column still contains unexpected characters.")
        print("     Check correlation_errors.csv — compare serial_char vs keylog_key.")
        print("  2. The two logs are from different runs.")
        return

    # ---- Clock offset -------------------------------------------------------
    valid, offset_ns = apply_clock_offset(valid)

    negative = valid[valid["t_latency_ms"] < 0]
    if len(negative) > 0:
        print(f"[WARN] {len(negative)} negative latency values after offset "
              f"correction — this is expected for events below the baseline.")

    # ---- Outlier removal ----------------------------------------------------
    clean, _ = remove_outliers(valid, sigma=args.sigma)
    print(f"      {len(clean)} clean events after outlier removal")

    # ---- Save correlated CSV ------------------------------------------------
    clean.to_csv("correlated.csv", index=False)
    print("[OUT] correlated.csv")

    # ---- Statistics ---------------------------------------------------------
    stats = compute_stats(clean)
    stats.to_csv("stats.csv", index=False)
    print("[OUT] stats.csv")
    print("\n--- Per-key latency statistics (ms) ---")
    print(stats.to_string(index=False))
    print()

    # ---- Cross-validation ---------------------------------------------------
    print("[...] Running cross-validation...")
    cv = crossvalidate(serial, args.press_ms, args.interval_ms)
    cv.to_csv("crossval.csv", index=False)
    max_delta  = cv["delta_us"].abs().max()
    mean_delta = cv["delta_us"].abs().mean()
    print(f"      Max |delta|: {max_delta:.1f} µs   "
          f"Mean |delta|: {mean_delta:.2f} µs")
    first_d = cv.sort_values("seq").iloc[:12]["delta_us"].mean()
    last_d  = cv.sort_values("seq").iloc[-12:]["delta_us"].mean()
    print(f"      Drift check — first 12: {first_d:.1f} µs  "
          f"last 12: {last_d:.1f} µs  "
          f"net: {last_d - first_d:.1f} µs")

    # ---- Plots --------------------------------------------------------------
    print("\n[...] Generating plots...")
    plot_histogram(clean)
    plot_boxplot_per_key(clean)
    plot_scatter_stability(clean)
    plot_pattern_overlay(clean, args.press_ms, args.interval_ms,
                         args.max_overlay_cycles)
    plot_crossval(cv)

    print("\n[DONE] All outputs written to current directory.")


if __name__ == "__main__":
    main()