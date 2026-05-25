"""
correlate.py — Iteration 3 correlation and visualisation engine
===============================================================

Usage:
    python correlate.py --serial  serial_YYYYMMDD_HHMMSS.csv \
                        --keylog  keyboard_YYYYMMDD_HHMMSS_filtered.csv \
                        --raw     keyboard_YYYYMMDD_HHMMSS_raw.csv

Outputs written to current directory:
    correlated.csv        — one row per matched keypress with t_latency_ms
    stats.csv             — per-key descriptive statistics
    outliers.csv          — rows removed by sigma rule
    crossval.csv          — cross-validation delta per press
    hist_latency.png      — latency distribution histogram
    boxplot_per_key.png   — box plot per letter
    scatter_stability.png — latency over time
    overlay_pattern.png   — keystroke dynamics overlay (horizontal)
    crossval.png          — actual Pico T0 vs expected T0
"""

import argparse
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ---------------------------------------------------------------------------
# Pattern
# ---------------------------------------------------------------------------
PATTERN     = list("FHBURGENLAND")
PATTERN_LEN = len(PATTERN)
PALETTE     = [
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
    return (series.astype(str)
            .str.strip().str.strip('"').str.strip("'").str.strip()
            .str.upper())


def load_serial(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, comment="#",
                     dtype={"seq": int, "cycle": int, "pos": int,
                            "gpio": int, "t0_us": int},
                     keep_default_na=False)
    df["char"]  = clean_keyname(df["char"])
    df["t0_ns"] = df["t0_us"].astype(np.int64) * 1000
    return df.sort_values("seq").reset_index(drop=True)


def load_keylog(path: str) -> pd.DataFrame:
    # Use cp1252 (Windows Western European) encoding because the C++ key logger
    # writes key names using GetKeyNameTextA which returns strings in the system
    # ANSI codepage. On German/Austrian Windows this encodes characters like
    # Strg, Umschalt, or Ü as Windows-1252 bytes rather than UTF-8.
    # errors="replace" ensures any remaining undecodable bytes become ? rather
    # than crashing, so a single bad key name never aborts the entire run.
    df = pd.read_csv(path, keep_default_na=False,
                     encoding="cp1252", encoding_errors="replace")
    df["keyname"] = clean_keyname(df["keyname"])
    df["t1_ns"]   = df["t1_ns"].astype(np.int64)

    # Filter out non-pattern keys (e.g. NACH-OBEN, EINGABE, STRG, C from
    # window switching or run termination). Keep only keys that appear
    # in the pattern so the greedy alignment is not misled by noise.
    pattern_set = set(PATTERN)
    n_before = len(df)
    df = df[df["keyname"].isin(pattern_set)].reset_index(drop=True)
    n_filtered = n_before - len(df)
    if n_filtered > 0:
        print(f"[LOAD] Filtered {n_filtered} non-pattern key(s) from keylog")

    return df

# ---------------------------------------------------------------------------
# Correlation — gap-aware greedy alignment
#
# Naive positional join (row N of serial = row N of keylog) breaks
# catastrophically when the OS misses a keypress: every subsequent row
# is shifted by one and all comparisons fail. This is what happened in
# UC3 where one mid-run stall propagated 5,759 false mismatches.
#
# Greedy alignment: walk both logs in order. When characters match,
# record the pair and advance both pointers. When they differ, assume
# the serial event was lost (not received by OS) and advance only the
# serial pointer. Record the gap. The result has one row per OS-received
# event, each correctly attributed to its generator origin, plus a
# separate list of dropped events.
# ---------------------------------------------------------------------------

def correlate(serial: pd.DataFrame, keylog: pd.DataFrame) -> pd.DataFrame:
    matched_rows = []
    dropped_rows = []
    si = ki = 0
    n_serial, n_keylog = len(serial), len(keylog)

    while si < n_serial and ki < n_keylog:
        s_row = serial.iloc[si]
        k_row = keylog.iloc[ki]
        if s_row["char"] == k_row["keyname"]:
            matched_rows.append({
                "seq":   int(s_row["seq"]),
                "cycle": int(s_row["cycle"]),
                "pos":   int(s_row["pos"]),
                "char":  s_row["char"],
                "gpio":  int(s_row["gpio"]),
                "t0_ns": int(s_row["t0_ns"]),
                "t1_ns": int(k_row["t1_ns"]),
            })
            si += 1
            ki += 1
        else:
            # Serial event has no matching keylog event — assume OS missed it
            dropped_rows.append({
                "seq":   int(s_row["seq"]),
                "cycle": int(s_row["cycle"]),
                "pos":   int(s_row["pos"]),
                "char":  s_row["char"],
                "next_keylog": k_row["keyname"],
            })
            si += 1

    leftover_serial = n_serial - si
    leftover_keylog = n_keylog - ki

    if dropped_rows:
        n_drop = len(dropped_rows)
        print(f"[WARN] {n_drop} serial event(s) had no matching OS event "
              f"({100*n_drop/n_serial:.3f}% drop rate). See dropped_events.csv")
        pd.DataFrame(dropped_rows).to_csv("dropped_events.csv", index=False)

    if leftover_serial > 0:
        print(f"[INFO] {leftover_serial} trailing serial event(s) ignored "
              f"(keylog ended first — likely run-end truncation)")
    if leftover_keylog > 0:
        print(f"[INFO] {leftover_keylog} trailing keylog event(s) ignored "
              f"(serial ended first)")

    print(f"[OK]  {len(matched_rows)} events matched")

    corr = pd.DataFrame(matched_rows)
    corr["t_latency_ns"] = corr["t1_ns"] - corr["t0_ns"]
    corr["t_latency_ms"] = corr["t_latency_ns"] / 1_000_000.0
    corr["valid"] = True
    return corr

# ---------------------------------------------------------------------------
# Clock offset correction + epoch sanity check
#
# The Pico t0 and Windows t1 have different clock origins so raw latency
# (t1 - t0) is a large constant number plus jitter and slow drift.
#
# Two kinds of discontinuity can appear in the raw latency series:
#
# 1. Timebase resets: a clock on one side restarts (Pico USB re-enumerates,
#    or Windows QPC reference shifts). Raw latency jumps by hundreds or
#    thousands of seconds. Beyond this point the constant offset is
#    different and a separate correction is needed.
#
# 2. Stalls: the OS misses one or more polling intervals, the host
#    timestamp falls behind by hundreds of milliseconds to a few seconds,
#    and either (a) the host catches up by delivering a burst of buffered
#    events with sub-millisecond inter-arrival, or (b) a small number of
#    events are lost and subsequent events resume at a permanently shifted
#    baseline. Stalls are jitter, not a clock reset, and must remain in
#    the data as outliers.
#
# We split into epochs only at jumps >5 seconds (clearly a reset). Within
# each epoch we fit drift using a trimmed linear regression — the trim
# excludes burst events from the fit so they appear correctly as outliers
# in the residuals rather than dragging the trend line.
# ---------------------------------------------------------------------------

def apply_clock_offset(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    df = df.copy().sort_values("seq").reset_index(drop=True)
    raw = df["t_latency_ns"].astype(np.int64)
    df["t_latency_ns_raw"] = raw

    # ---- Detect epoch boundaries: timebase resets + sustained steps -------
    # We only split at jumps that produce a sustained baseline change. A
    # transient excursion (host stalls then catches up via a buffered burst,
    # then returns to the prior baseline) is a single epoch with outliers,
    # not a new epoch.
    #
    # Test: compare the median of N events before and after a candidate jump.
    # If they differ by more than the jump magnitude / 4 we treat it as
    # sustained. This separates "stall + permanent shift" (split) from
    # "stall + burst recovery" (don't split).
    RESET_THRESHOLD_NS = 5_000_000_000   # 5 seconds — always a reset
    STEP_THRESHOLD_NS  = 200_000_000     # 200 ms — candidate stall step
    LOOKAROUND         = 50              # events on each side to compare

    diffs = raw.diff().abs()
    candidates = diffs[diffs > STEP_THRESHOLD_NS].index.tolist()
    boundary_indices = []
    for idx in candidates:
        jump = diffs.loc[idx]
        # Always split at very large jumps (timebase resets)
        if jump > RESET_THRESHOLD_NS:
            boundary_indices.append(idx)
            continue
        # For smaller jumps, check whether the baseline truly shifted
        before = raw.iloc[max(0, idx - LOOKAROUND):idx]
        after  = raw.iloc[idx:min(len(raw), idx + LOOKAROUND)]
        if len(before) < 5 or len(after) < 5:
            continue
        shift = abs(after.median() - before.median())
        if shift > jump / 4:
            boundary_indices.append(idx)

    if boundary_indices:
        for idx in boundary_indices:
            jump = diffs.loc[idx] / 1e6
            seq = int(df.loc[idx, "seq"])
            kind = "timebase reset" if diffs.loc[idx] > RESET_THRESHOLD_NS \
                                    else "sustained baseline step"
            print(f"[WARN] Epoch boundary at seq={seq} ({kind}, "
                  f"jump = {jump:.1f} ms).")
        boundaries = [0] + boundary_indices + [len(df)]
        epochs = []
        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            epoch = df.iloc[start:end].copy()
            if len(epoch) > 0:
                epochs.append(epoch)
    else:
        epochs = [df.copy()]

    # ---- Robust drift fit per epoch ----------------------------------------
    def correct_single_epoch(edf, epoch_num):
        raw_e = edf["t_latency_ns"].astype(np.float64).values
        seq_e = edf["seq"].astype(np.float64).values

        if len(edf) < 100:
            # Too short for trimmed fit — just demean
            slope, intercept = 0.0, float(np.median(raw_e))
        else:
            # Trimmed regression: use only events between 10th and 90th
            # percentile of raw latency for fitting. This excludes both
            # bursts (above the 90th) and any negative outliers (below the
            # 10th), giving a clean drift line through the bulk of the data.
            lo, hi = np.quantile(raw_e, [0.10, 0.90])
            mask = (raw_e >= lo) & (raw_e <= hi)
            slope, intercept = np.polyfit(seq_e[mask], raw_e[mask], 1)

        trend    = slope * seq_e + intercept
        residual = raw_e - trend
        # Centre on first 12 events of this epoch (after trend removal)
        anchor   = float(np.median(residual[:12]))
        corrected = residual - anchor

        drift_ppm = (slope / 1000.0) / 0.1
        total_presses = seq_e[-1] - seq_e[0] if len(seq_e) > 1 else 0
        total_drift_ms = (slope * total_presses) / 1_000_000.0

        print(f"[INFO] Epoch {epoch_num}: {len(edf)} events, "
              f"drift={drift_ppm:.1f} ppm, "
              f"total drift={total_drift_ms:.2f} ms over "
              f"{int(total_presses)} presses")

        edf = edf.copy()
        edf["t_latency_ns"]       = corrected.astype(np.int64)
        edf["t_latency_ms"]       = corrected / 1_000_000.0
        edf["t_latency_ns_drift"] = (raw_e - trend).astype(np.int64)
        return edf, slope

    corrected_epochs = []
    all_slopes = []
    for i, edf in enumerate(epochs):
        if len(edf) < 12:
            print(f"[WARN] Epoch {i+1} has only {len(edf)} events — skipping.")
            continue
        cedf, slope = correct_single_epoch(edf, i + 1)
        corrected_epochs.append(cedf)
        all_slopes.append(slope)

    df_out = pd.concat(corrected_epochs).sort_values("seq").reset_index(drop=True)

    mean_slope = float(np.mean(all_slopes)) if all_slopes else 0.0
    print(f"[INFO] Latency range after correction: "
          f"{df_out['t_latency_ms'].min():.2f} ms  to  "
          f"{df_out['t_latency_ms'].max():.2f} ms")

    return df_out, mean_slope

# ---------------------------------------------------------------------------
# Outlier removal — per-key 3-sigma
# ---------------------------------------------------------------------------

def remove_outliers(df: pd.DataFrame,
                    col: str = "t_latency_ms",
                    sigma: float = 3.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Trim per-key outliers using a 3-sigma rule. Grouped by (pos, char) so
    the two pattern occurrences of 'N' are treated as separate distributions.
    """
    clean_parts, outlier_parts = [], []
    seen = set()
    for pos, char in enumerate(PATTERN):
        key = (pos, char)
        if key in seen:
            continue
        seen.add(key)
        grp = df[(df["pos"] == pos) & (df["char"] == char)]
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

    clean = (pd.concat(clean_parts).sort_values("seq")
               .reset_index(drop=True))
    if outlier_parts:
        outliers = (pd.concat(outlier_parts).sort_values("seq")
                      .reset_index(drop=True))
        if len(outliers) > 0:
            print(f"[INFO] {len(outliers)} outlier(s) removed (>{sigma}σ per key). "
                  f"See outliers.csv")
            outliers.to_csv("outliers.csv", index=False)
    else:
        outliers = pd.DataFrame()
    return clean, outliers

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-key statistics, grouped by (pos, char). The pattern contains
    'N' twice (positions 7 and 10) which is the same physical key but
    appears in different pattern slots; reporting them separately lets
    the reader see whether the two pattern occurrences agree, which is
    a useful check on the scan-matrix offset hypothesis.
    """
    rows = []
    seen = set()
    # Iterate in pattern order so output rows match the typed sequence
    for pos, char in enumerate(PATTERN):
        key = (pos, char)
        if key in seen:
            continue
        seen.add(key)
        grp = df[(df["pos"] == pos) & (df["char"] == char)]["t_latency_ms"]
        if len(grp) == 0:
            continue
        rows.append({
            "pos":       pos,
            "char":      char,
            "count":     len(grp),
            "mean_ms":   round(grp.mean(),         4),
            "median_ms": round(grp.median(),       4),
            "std_ms":    round(grp.std(),          4),
            "min_ms":    round(grp.min(),          4),
            "max_ms":    round(grp.max(),          4),
            "p5_ms":     round(grp.quantile(0.05), 4),
            "p95_ms":    round(grp.quantile(0.95), 4),
        })
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------

def crossvalidate(serial: pd.DataFrame,
                  press_ms: int = 30,
                  interval_ms: int = 70) -> pd.DataFrame:
    period_us = (press_ms + interval_ms) * 1000
    rows = []
    for cycle_id, grp in serial.groupby("cycle"):
        grp = grp.sort_values("pos")
        starts = grp[grp["pos"] == 0]["t0_us"].values
        if len(starts) == 0:
            continue
        t0_start = int(starts[0])
        for _, row in grp.iterrows():
            expected = t0_start + int(row["pos"]) * period_us
            rows.append({
                "seq":            int(row["seq"]),
                "cycle":          cycle_id,
                "pos":            int(row["pos"]),
                "char":           row["char"],
                "t0_us":          int(row["t0_us"]),
                "t0_expected_us": expected,
                "delta_us":       int(row["t0_us"]) - expected,
            })
    return pd.DataFrame(rows)

# ---------------------------------------------------------------------------
# Plot 1 — Histogram
# Clamp x-axis to ±50ms to prevent a single outlier from collapsing the plot
# ---------------------------------------------------------------------------

def plot_histogram(df: pd.DataFrame, out: str = "hist_latency.png") -> None:
    lat  = df["t_latency_ms"]
    xmin = max(-20, lat.quantile(0.001) - 1)
    xmax = min(50,  lat.quantile(0.999) + 1)
    bins = np.arange(xmin, xmax + 0.5, 0.5)

    fig, ax = plt.subplots(figsize=(11, 5))
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

# ---------------------------------------------------------------------------
# Plot 2 — Box plot per key
# Force y-axis to ±50ms so one outlier cannot collapse every box to a line
# ---------------------------------------------------------------------------

def plot_boxplot_per_key(df: pd.DataFrame,
                         out: str = "boxplot_per_key.png") -> None:
    fig, ax = plt.subplots(figsize=(13, 5))
    # One box per pattern position (so the two N's get separate boxes)
    data   = [df[(df["pos"] == p) & (df["char"] == c)]["t_latency_ms"].values
              for p, c in enumerate(PATTERN)]
    colors = [CHAR_COLOR[c] for c in PATTERN]

    bp = ax.boxplot(data, patch_artist=True, notch=False,
                    medianprops=dict(color="black", linewidth=1.5),
                    whiskerprops=dict(linewidth=1),
                    capprops=dict(linewidth=1),
                    flierprops=dict(marker="o", markersize=2,
                                   alpha=0.3, linestyle="none"))
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    # Compute a sensible y range from the actual IQR across all keys
    all_vals = df["t_latency_ms"]
    y_lo = max(-50, all_vals.quantile(0.005) - 2)
    y_hi = min(50,  all_vals.quantile(0.995) + 2)
    ax.set_ylim(y_lo, y_hi)

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

# ---------------------------------------------------------------------------
# Plot 3 — Scatter stability
# Two panels: full y-range to show all outliers/bursts, then clipped to
# ±35ms to make normal jitter visible. A red cycle-mean line on the
# clipped panel reveals baseline drift between cycles.
# ---------------------------------------------------------------------------

def plot_scatter_stability(df: pd.DataFrame,
                           out: str = "scatter_stability.png") -> None:
    plot_df = df
    if len(df) > 5000:
        plot_df = df.sample(n=5000, random_state=42).sort_values("seq")
        print(f"[INFO] Scatter plot: subsampled to 5000 of {len(df)} points "
              f"for readability")

    fig, axes = plt.subplots(2, 1, figsize=(13, 8))

    # ---- Top panel: full y range ----
    ax = axes[0]
    for char in PATTERN:
        grp = plot_df[plot_df["char"] == char]
        ax.scatter(grp["seq"], grp["t_latency_ms"],
                   s=3, alpha=0.35, color=CHAR_COLOR[char], label=char,
                   linewidths=0)
    ax.axhline(0, color="black", linewidth=0.5, alpha=0.5)
    ax.set_ylabel("Latency (ms)", fontsize=11)
    ax.set_title("Latency stability — full y-range (shows bursts and tail events)",
                 fontsize=12)
    ax.legend(title="Key", ncol=6, fontsize=7, markerscale=2, loc="upper right")
    ax.grid(alpha=0.2)

    # ---- Bottom panel: clipped y range with cycle mean ----
    ax = axes[1]
    for char in PATTERN:
        grp = plot_df[plot_df["char"] == char]
        ax.scatter(grp["seq"], grp["t_latency_ms"],
                   s=3, alpha=0.35, color=CHAR_COLOR[char],
                   linewidths=0)
    # Cycle mean line
    cycle_mean = df.groupby("cycle")["t_latency_ms"].mean()
    cycle_seq  = df.groupby("cycle")["seq"].mean()
    ax.plot(cycle_seq, cycle_mean, color="#C44E52", linewidth=0.8,
            alpha=0.8, label="cycle mean")
    ax.axhline(0, color="black", linewidth=0.5, alpha=0.5)
    ax.set_ylim(-15, 35)
    ax.set_xlabel("Sequence number", fontsize=11)
    ax.set_ylabel("Latency (ms)", fontsize=11)
    ax.set_title("Same data, y clipped to ±35 ms (shows normal jitter and baseline drift)",
                 fontsize=12)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.2)

    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")

# ---------------------------------------------------------------------------
# Plot 4 — Keystroke dynamics overlay (redesigned for large datasets)
#
# Instead of one row per cycle (which becomes an unreadably tall image),
# this version plots one COLUMN per key position. For each key, all cycle
# arrival times are shown as a horizontal strip of dots, offset-corrected
# so the expected arrival sits at x=0. This makes the jitter per key
# immediately visible as horizontal spread, and scales perfectly to
# 1000 cycles because each key column has a fixed height.
# ---------------------------------------------------------------------------

def plot_pattern_overlay(df: pd.DataFrame,
                         press_ms: int = 30,
                         interval_ms: int = 70,
                         max_cycles: int = 1000,
                         out: str = "overlay_pattern.png") -> None:
    period_ms = press_ms + interval_ms

    # Compute per-cycle, per-key latency relative to expected arrival
    # Expected arrival of key at position p = p * period_ms (ms into cycle)
    # Actual arrival = t1_ns relative to first key of same cycle, in ms
    rows = []
    for cycle_id, grp in df.groupby("cycle"):
        if cycle_id >= max_cycles:
            break
        grp = grp.sort_values("pos")
        if len(grp) == 0:
            continue
        first_t1 = grp["t1_ns"].min()
        for _, row in grp.iterrows():
            actual_ms   = (row["t1_ns"] - first_t1) / 1_000_000.0
            expected_ms = row["pos"] * period_ms
            jitter_ms   = actual_ms - expected_ms
            rows.append({
                "cycle": cycle_id,
                "pos":   row["pos"],
                "char":  row["char"],
                "jitter_ms": jitter_ms,
            })

    if not rows:
        print(f"[WARN] No data for overlay plot")
        return

    ov = pd.DataFrame(rows)

    fig, ax = plt.subplots(figsize=(14, 5))

    # Plot each key as a horizontal strip of dots
    for pos, char in enumerate(PATTERN):
        grp = ov[ov["pos"] == pos]["jitter_ms"]
        if len(grp) == 0:
            continue
        # y position = pattern position index
        ax.scatter(grp, [pos] * len(grp),
                   s=4, alpha=0.25, color=CHAR_COLOR[char],
                   linewidths=0)
        # Mark the median jitter for this key
        med = grp.median()
        ax.plot(med, pos, marker="|", color=CHAR_COLOR[char],
                markersize=12, markeredgewidth=2)

    ax.axvline(0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_yticks(range(len(PATTERN)))
    ax.set_yticklabels(PATTERN, fontsize=10)
    ax.set_xlabel("Jitter relative to expected arrival (ms)", fontsize=12)
    ax.set_title("Keystroke dynamics overlay — per-key OS jitter across all cycles",
                 fontsize=13)

    # Clamp x-axis to ±20ms so structure is visible
    jitter_range = ov["jitter_ms"]
    x_lo = max(-20, jitter_range.quantile(0.005) - 1)
    x_hi = min(20,  jitter_range.quantile(0.995) + 1)
    ax.set_xlim(x_lo, x_hi)
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")

# ---------------------------------------------------------------------------
# Plot 5 — Cross-validation
# ---------------------------------------------------------------------------

def plot_crossval(cv: pd.DataFrame, out: str = "crossval.png") -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 6), sharex=True)

    for char in PATTERN:
        grp = cv[cv["char"] == char]
        if len(grp) == 0:
            continue
        ax1.scatter(grp["seq"], grp["delta_us"],
                    s=3, alpha=0.4, color=CHAR_COLOR[char],
                    label=char, linewidths=0)

    ax1.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax1.set_ylabel("T0 delta (µs)\nactual − expected", fontsize=10)
    ax1.set_title("Cross-validation: actual Pico T0 vs pattern-expected T0",
                  fontsize=12)
    ax1.legend(title="Key", ncol=4, fontsize=7, markerscale=2)
    ax1.grid(alpha=0.2)

    cv_s    = cv.sort_values("seq")
    rolling = (cv_s["delta_us"].abs()
                .rolling(window=12, center=True, min_periods=1)
                .mean())
    ax2.plot(cv_s["seq"], rolling, color="#4C72B0", linewidth=0.8)
    ax2.set_xlabel("Sequence number", fontsize=10)
    ax2.set_ylabel("Rolling |delta| (µs)\n12-key window", fontsize=10)
    ax2.grid(alpha=0.2)

    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")

# ---------------------------------------------------------------------------
# UC4 — Keystroke dynamics features (dwell time, flight time)
#
# Dwell time = time from a key's DOWN event to its matching UP event,
#              for the same physical key. Ground truth is the press
#              duration configured in the generator firmware (default 30 ms).
#
# Flight time = time from a key's DOWN event to the next key's DOWN event.
#               Ground truth is the cycle period (press_ms + interval_ms,
#               default 100 ms).
#
# Both features are intervals between two events on the same clock (the host
# QPC), so they do NOT require cross-clock correction. They are absolute
# measurements against the generator ground truth, not relative to a
# per-run baseline.
# ---------------------------------------------------------------------------

def load_raw_keylog(path: str) -> pd.DataFrame:
    """Raw keylog has DOWN and UP events. Same encoding handling as filtered."""
    df = pd.read_csv(path, keep_default_na=False,
                     encoding="cp1252", encoding_errors="replace")
    df["keyname"] = clean_keyname(df["keyname"])
    df["t1_ns"]   = df["t1_ns"].astype(np.int64)
    df["edge"]    = df["edge"].astype(str).str.strip().str.upper()

    pattern_set = set(PATTERN)
    n_before = len(df)
    df = df[df["keyname"].isin(pattern_set)].reset_index(drop=True)
    n_filtered = n_before - len(df)
    if n_filtered > 0:
        print(f"[LOAD] Filtered {n_filtered} non-pattern key event(s) from raw log")
    return df


def fix_qpc_resets(df: pd.DataFrame, time_col: str = "t1_ns",
                   threshold: int = 1_000_000_000_000) -> pd.DataFrame:
    """Patch any timebase resets in the keylog by re-adding the offset.
    A reset is detected when t1_ns drops by more than 1000 seconds."""
    df = df.copy()
    diffs = df[time_col].diff()
    reset_idx = diffs[diffs.abs() > threshold].index.tolist()
    for idx in reset_idx:
        offset = df.iloc[idx-1][time_col] - df.iloc[idx][time_col] + 100_000_000
        df.loc[idx:, time_col] = df.loc[idx:, time_col] + offset
        print(f"[INFO] Patched timebase reset in keylog at row {idx} "
              f"(restored offset = {offset/1e9:.1f} s)")
    return df


def compute_dynamics(raw: pd.DataFrame,
                     press_ms: float = 30.0,
                     interval_ms: float = 70.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute dwell and flight times from a raw DOWN+UP keylog.

    Returns (dwell_df, flight_df). Each row in dwell_df pairs one DOWN with
    the next UP for the same vkey. Each row in flight_df pairs two
    consecutive DOWN events.
    """
    raw = fix_qpc_resets(raw)
    raw_sorted = raw.sort_values("t1_ns").reset_index(drop=True)

    # ---- Dwell -------------------------------------------------------------
    # For each DOWN, walk forward to find the next UP with the same vkey.
    # Using index-based pairing (not time-based) handles overlapping events
    # correctly: if A_DOWN, B_DOWN, A_UP, B_UP, then A_DOWN pairs with A_UP
    # and B_DOWN with B_UP, not A_DOWN with B_UP.
    pending = {}   # vkey -> list of (idx, t1_ns) waiting for an UP
    dwells  = []
    for i, row in raw_sorted.iterrows():
        vk = row["vkey"]
        if row["edge"] == "DOWN":
            pending.setdefault(vk, []).append((i, int(row["t1_ns"]),
                                               row["keyname"]))
        elif row["edge"] == "UP":
            queue = pending.get(vk)
            if queue:
                d_idx, d_t, d_char = queue.pop(0)
                u_t = int(row["t1_ns"])
                dwells.append({
                    "down_idx":  d_idx,
                    "char":      d_char,
                    "down_t_ns": d_t,
                    "up_t_ns":   u_t,
                    "dwell_ms":  (u_t - d_t) / 1e6,
                })

    dwell_df = pd.DataFrame(dwells)
    n_unmatched = sum(len(q) for q in pending.values())
    if n_unmatched > 0:
        print(f"[WARN] {n_unmatched} DOWN event(s) had no matching UP "
              f"(probably truncated at run end)")

    print(f"[OK]  {len(dwell_df)} dwell pairs computed")
    print(f"      Ground truth dwell (from press_ms): {press_ms:.1f} ms")
    print(f"      Mean: {dwell_df['dwell_ms'].mean():.2f} ms "
          f"(offset {dwell_df['dwell_ms'].mean()-press_ms:+.2f} ms)")
    print(f"      Std:  {dwell_df['dwell_ms'].std():.2f} ms")
    print(f"      Relative uncertainty: "
          f"{100*dwell_df['dwell_ms'].std()/press_ms:.1f}%")

    # ---- Flight ------------------------------------------------------------
    downs = raw_sorted[raw_sorted["edge"] == "DOWN"].reset_index(drop=True)
    downs["flight_ms"] = downs["t1_ns"].diff() / 1e6
    flight_df = downs.iloc[1:].copy()  # drop first (no preceding DOWN)

    # Cycle period for flight ground truth
    cycle_ms = press_ms + interval_ms
    print(f"\n[OK]  {len(flight_df)} flight intervals computed")
    print(f"      Ground truth flight (cycle period): {cycle_ms:.1f} ms")
    print(f"      Mean: {flight_df['flight_ms'].mean():.3f} ms "
          f"(offset {flight_df['flight_ms'].mean()-cycle_ms:+.3f} ms)")
    print(f"      Std:  {flight_df['flight_ms'].std():.3f} ms")
    print(f"      Relative uncertainty: "
          f"{100*flight_df['flight_ms'].std()/cycle_ms:.2f}%")

    return dwell_df, flight_df


def compute_dynamics_stats(dwell_df: pd.DataFrame,
                           flight_df: pd.DataFrame,
                           press_ms: float,
                           cycle_ms: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-key statistics for dwell and flight, with ground-truth offsets."""
    dwell_rows = []
    for char in PATTERN:
        if char in [r["char"] for r in dwell_rows]:
            continue
        g = dwell_df[dwell_df["char"] == char]["dwell_ms"]
        if len(g) == 0:
            continue
        dwell_rows.append({
            "char":          char,
            "count":         len(g),
            "mean_ms":       round(g.mean(),    3),
            "median_ms":     round(g.median(),  3),
            "std_ms":        round(g.std(),     3),
            "offset_vs_gt":  round(g.mean() - press_ms, 3),
            "min_ms":        round(g.min(),     3),
            "max_ms":        round(g.max(),     3),
        })
    flight_rows = []
    for char in PATTERN:
        if char in [r["char"] for r in flight_rows]:
            continue
        g = flight_df[flight_df["keyname"] == char]["flight_ms"]
        if len(g) == 0:
            continue
        flight_rows.append({
            "char":          char,
            "count":         len(g),
            "mean_ms":       round(g.mean(),    3),
            "median_ms":     round(g.median(),  3),
            "std_ms":        round(g.std(),     3),
            "offset_vs_gt":  round(g.mean() - cycle_ms, 3),
            "min_ms":        round(g.min(),     3),
            "max_ms":        round(g.max(),     3),
        })
    return pd.DataFrame(dwell_rows), pd.DataFrame(flight_rows)


def plot_dynamics(dwell_df: pd.DataFrame, flight_df: pd.DataFrame,
                  press_ms: float, cycle_ms: float,
                  out: str = "dynamics_dwell_flight.png") -> None:
    """Two-panel comparison: dwell distribution vs flight distribution,
    each annotated with the ground truth and the relative uncertainty.
    """
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    # ---- Dwell box plot per key ----
    ax = axes[0]
    data = [dwell_df[dwell_df["char"] == c]["dwell_ms"].values
            for c in PATTERN if c in dwell_df["char"].unique()]
    labels = [c for c in PATTERN if c in dwell_df["char"].unique()]
    colors = [CHAR_COLOR[c] for c in labels]
    bp = ax.boxplot(data, patch_artist=True, notch=False,
                    medianprops=dict(color="black", linewidth=1.5),
                    flierprops=dict(marker=".", markersize=3, alpha=0.4))
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c)
        patch.set_alpha(0.7)
    ax.axhline(press_ms, color="red", linestyle="--", linewidth=1.5,
               label=f"Generator ground truth ({press_ms:.0f} ms)")
    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels)
    ax.set_xlabel("Key")
    ax.set_ylabel("Dwell time (ms)")
    rel_unc = 100 * dwell_df["dwell_ms"].std() / press_ms
    ax.set_title(f"Dwell time per key — overall mean {dwell_df['dwell_ms'].mean():.1f} ms, "
                 f"std {dwell_df['dwell_ms'].std():.1f} ms ({rel_unc:.0f}% rel.)")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # ---- Flight box plot per key ----
    ax = axes[1]
    data = [flight_df[flight_df["keyname"] == c]["flight_ms"].values
            for c in PATTERN if c in flight_df["keyname"].unique()]
    labels = [c for c in PATTERN if c in flight_df["keyname"].unique()]
    colors = [CHAR_COLOR[c] for c in labels]
    bp = ax.boxplot(data, patch_artist=True, notch=False,
                    medianprops=dict(color="black", linewidth=1.5),
                    flierprops=dict(marker=".", markersize=3, alpha=0.4))
    for patch, c in zip(bp["boxes"], colors):
        patch.set_facecolor(c)
        patch.set_alpha(0.7)
    ax.axhline(cycle_ms, color="red", linestyle="--", linewidth=1.5,
               label=f"Generator ground truth ({cycle_ms:.0f} ms)")
    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels)
    ax.set_xlabel("Key (flight TO this key)")
    ax.set_ylabel("Flight time (ms)")
    rel_unc = 100 * flight_df["flight_ms"].std() / cycle_ms
    ax.set_title(f"Flight time per key — overall mean {flight_df['flight_ms'].mean():.2f} ms, "
                 f"std {flight_df['flight_ms'].std():.2f} ms ({rel_unc:.1f}% rel.)")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_dynamics_histograms(dwell_df: pd.DataFrame, flight_df: pd.DataFrame,
                             press_ms: float, cycle_ms: float,
                             out: str = "dynamics_histograms.png") -> None:
    """Two-panel histogram showing dwell and flight distributions overlaid
    per-key, with ground truth markers."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 4))

    # Dwell
    ax = axes[0]
    chars_present = [c for c in PATTERN if c in dwell_df["char"].unique()]
    seen = set()
    for c in chars_present:
        if c in seen: continue
        seen.add(c)
        g = dwell_df[dwell_df["char"] == c]["dwell_ms"]
        ax.hist(g, bins=40, alpha=0.45, label=c, color=CHAR_COLOR[c])
    ax.axvline(press_ms, color="red", linestyle="--", linewidth=1.5,
               label=f"GT {press_ms:.0f} ms")
    ax.set_xlabel("Dwell time (ms)")
    ax.set_ylabel("Count")
    ax.set_title("Dwell time distribution by key")
    ax.legend(ncol=4, fontsize=8)
    ax.grid(alpha=0.3)

    # Flight
    ax = axes[1]
    seen = set()
    for c in chars_present:
        if c in seen: continue
        seen.add(c)
        g = flight_df[flight_df["keyname"] == c]["flight_ms"]
        ax.hist(g, bins=40, alpha=0.45, label=c, color=CHAR_COLOR[c])
    ax.axvline(cycle_ms, color="red", linestyle="--", linewidth=1.5,
               label=f"GT {cycle_ms:.0f} ms")
    ax.set_xlabel("Flight time (ms)")
    ax.set_ylabel("Count")
    ax.set_title("Flight time distribution by key")
    ax.legend(ncol=4, fontsize=8)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def run_dynamics_mode(raw_path: str, press_ms: float, interval_ms: float):
    """UC4 entry point: dwell/flight analysis from the raw keylog."""
    print(f"\n[IN]  Raw keylog (DOWN+UP): {raw_path}")
    raw = load_raw_keylog(raw_path)
    n_down = (raw["edge"] == "DOWN").sum()
    n_up   = (raw["edge"] == "UP").sum()
    print(f"      {len(raw)} events ({n_down} DOWN, {n_up} UP)")

    print("\n[...] Computing dwell and flight times...")
    dwell_df, flight_df = compute_dynamics(raw, press_ms, interval_ms)

    cycle_ms = press_ms + interval_ms
    dwell_stats, flight_stats = compute_dynamics_stats(
        dwell_df, flight_df, press_ms, cycle_ms)

    dwell_df.to_csv("dwell_events.csv", index=False)
    flight_df[["keyname", "t1_ns", "flight_ms"]].to_csv(
        "flight_events.csv", index=False)
    dwell_stats.to_csv("dwell_stats.csv", index=False)
    flight_stats.to_csv("flight_stats.csv", index=False)
    print("[OUT] dwell_events.csv, flight_events.csv, "
          "dwell_stats.csv, flight_stats.csv")

    print("\n--- Dwell statistics per key (ground truth = "
          f"{press_ms:.1f} ms) ---")
    print(dwell_stats.to_string(index=False))

    print(f"\n--- Flight statistics per key (ground truth = "
          f"{cycle_ms:.1f} ms) ---")
    print(flight_stats.to_string(index=False))

    print("\n[...] Generating plots...")
    plot_dynamics(dwell_df, flight_df, press_ms, cycle_ms)
    plot_dynamics_histograms(dwell_df, flight_df, press_ms, cycle_ms)

    print("\n[DONE]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlate generator serial log with OS key logger output, "
                    "or compute keystroke-dynamics features (UC4).")
    parser.add_argument("--mode", choices=["latency", "dynamics"],
                        default="latency",
                        help="latency: per-event T0->T1 jitter (UC1-3). "
                             "dynamics: dwell and flight time vs ground "
                             "truth (UC4). dynamics requires --raw.")
    parser.add_argument("--serial",  required=False)
    parser.add_argument("--keylog",  required=False)
    parser.add_argument("--raw",     required=False, default=None)
    parser.add_argument("--press-ms",            type=int,   default=30)
    parser.add_argument("--interval-ms",         type=int,   default=70)
    parser.add_argument("--sigma",               type=float, default=3.0)
    parser.add_argument("--max-overlay-cycles",  type=int,   default=1000)
    args = parser.parse_args()

    if args.mode == "dynamics":
        if not args.raw:
            parser.error("--mode dynamics requires --raw <raw_keylog.csv>")
        run_dynamics_mode(args.raw, args.press_ms, args.interval_ms)
        return

    # ---- Latency mode (UC1-3) ----
    if not args.serial or not args.keylog:
        parser.error("--mode latency requires --serial and --keylog")

    print(f"\n[IN]  Serial log  : {args.serial}")
    serial = load_serial(args.serial)
    print(f"      {len(serial)} generator events  "
          f"| first: '{serial['char'].iloc[0]}'  "
          f"last: '{serial['char'].iloc[-1]}'")

    print(f"[IN]  Key log     : {args.keylog}")
    keylog = load_keylog(args.keylog)
    print(f"      {len(keylog)} OS key events  "
          f"| first: '{keylog['keyname'].iloc[0]}'  "
          f"last: '{keylog['keyname'].iloc[-1]}'")

    print("[...] Correlating (greedy gap-aware alignment)...")
    corr = correlate(serial, keylog)

    if len(corr) == 0:
        print("\n[ERROR] No matched events.")
        return

    print("[...] Applying clock offset correction...")
    corr, offset_ns = apply_clock_offset(corr)

    # Save the FULL corrected dataset including all outliers and burst events
    corr.to_csv("correlated_all.csv", index=False)
    print("[OUT] correlated_all.csv (all matched events, including outliers)")

    # Also save a 3-sigma-trimmed version for distribution stats
    clean, outliers = remove_outliers(corr, sigma=args.sigma)
    print(f"      {len(clean)} events within 3-sigma per-key, "
          f"{len(outliers)} outside")
    clean.to_csv("correlated.csv", index=False)
    print("[OUT] correlated.csv (3-sigma trimmed for distribution stats)")

    stats = compute_stats(clean)
    stats.to_csv("stats.csv", index=False)
    print("[OUT] stats.csv")
    print("\n--- Per-key latency statistics (ms, 3-sigma trimmed) ---")
    print(stats.to_string(index=False))

    # Also report the un-trimmed worst-case events
    extreme = corr.nlargest(20, "t_latency_ms")[
        ["seq","cycle","pos","char","t_latency_ms"]]
    extreme.to_csv("extreme_events.csv", index=False)
    print("\n[OUT] extreme_events.csv (top 20 latency events, untrimmed)")
    print(extreme.to_string(index=False))

    print("\n[...] Cross-validation...")
    cv = crossvalidate(serial, args.press_ms, args.interval_ms)
    cv.to_csv("crossval.csv", index=False)
    print(f"      Max |delta|: {cv['delta_us'].abs().max():.1f} µs  "
          f"Mean |delta|: {cv['delta_us'].abs().mean():.2f} µs")
    first_d = cv.sort_values("seq").iloc[:12]["delta_us"].mean()
    last_d  = cv.sort_values("seq").iloc[-12:]["delta_us"].mean()
    print(f"      Drift: first 12 avg {first_d:.1f} µs  "
          f"→  last 12 avg {last_d:.1f} µs  "
          f"(net {last_d - first_d:.1f} µs)")

    print("\n[...] Generating plots...")
    plot_histogram(clean)
    plot_boxplot_per_key(clean)
    plot_scatter_stability(corr)   # use untrimmed so bursts are visible
    plot_pattern_overlay(clean, args.press_ms, args.interval_ms,
                         args.max_overlay_cycles)
    plot_crossval(cv)

    print("\n[DONE]")


if __name__ == "__main__":
    main()