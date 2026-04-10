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
    return df.reset_index(drop=True)

# ---------------------------------------------------------------------------
# Sequence alignment
# ---------------------------------------------------------------------------
SEARCH_WINDOW = 120   # 10 full pattern cycles — handles large start offsets

def align_sequences(serial: pd.DataFrame,
                    keylog: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    s_chars = serial["char"].tolist()
    k_chars = keylog["keyname"].tolist()

    best_offset, best_score = 0, -1
    for offset in range(SEARCH_WINDOW + 1):
        n = min(len(s_chars), len(k_chars) - offset)
        if n <= 0:
            break
        score = sum(s == k for s, k in
                    zip(s_chars[:n], k_chars[offset:offset + n]))
        if score > best_score:
            best_score, best_offset = score, offset

    if best_offset > 0:
        print(f"[ALIGN] Trimmed {best_offset} leading row(s) from keylog")
        keylog = keylog.iloc[best_offset:].reset_index(drop=True)
        return serial, keylog

    best_offset, best_score = 0, -1
    for offset in range(1, SEARCH_WINDOW + 1):
        n = min(len(s_chars) - offset, len(k_chars))
        if n <= 0:
            break
        score = sum(s == k for s, k in
                    zip(s_chars[offset:offset + n], k_chars[:n]))
        if score > best_score:
            best_score, best_offset = score, offset

    if best_offset > 0:
        print(f"[ALIGN] Trimmed {best_offset} leading row(s) from serial log")
        serial = serial.iloc[best_offset:].reset_index(drop=True)

    return serial, keylog

# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def correlate(serial: pd.DataFrame, keylog: pd.DataFrame) -> pd.DataFrame:
    n = min(len(serial), len(keylog))
    if len(serial) != len(keylog):
        print(f"[WARN] Row count mismatch: serial={len(serial)}, "
              f"keylog={len(keylog)}. Using first {n} rows.")

    s = serial.iloc[:n].reset_index(drop=True)
    k = keylog.iloc[:n].reset_index(drop=True)

    mismatch_mask = s["char"] != k["keyname"]
    n_miss = int(mismatch_mask.sum())

    if n_miss > 0:
        print(f"[WARN] {n_miss} character mismatches. See correlation_errors.csv")
        pd.DataFrame({
            "row":         mismatch_mask[mismatch_mask].index.tolist(),
            "serial_char": s.loc[mismatch_mask, "char"].tolist(),
            "keylog_key":  k.loc[mismatch_mask, "keyname"].tolist(),
        }).to_csv("correlation_errors.csv", index=False)
    else:
        print(f"[OK]  All {n} rows matched cleanly")

    t0 = s["t0_ns"].astype(np.int64)
    t1 = k["t1_ns"].astype(np.int64)

    corr = pd.DataFrame({
        "seq":          s["seq"],
        "cycle":        s["cycle"],
        "pos":          s["pos"],
        "char":         s["char"],
        "gpio":         s["gpio"],
        "t0_ns":        t0,
        "t1_ns":        t1,
        "t_latency_ns": t1 - t0,
        "valid":        ~mismatch_mask,
    })
    corr["t_latency_ms"] = corr["t_latency_ns"] / 1_000_000.0
    return corr

# ---------------------------------------------------------------------------
# Clock offset correction + epoch sanity check
#
# The Pico t0 and Windows t1 have different clock origins so raw latency is
# a large number. We subtract the median of the first cycle as a baseline.
#
# If the data contains rows from two different recording sessions (e.g. the
# Windows QPC reset, or the run was stopped and restarted) the raw latency
# values will form two distinct populations separated by millions of ms.
# We detect this with an IQR-based gate BEFORE applying the offset so that
# only the dominant population contributes to the baseline calculation, and
# rows from the other population are dropped with a clear warning.
# ---------------------------------------------------------------------------

def apply_clock_offset(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """
    Correction pipeline with automatic Pico reset detection.

    The Pico can reset mid-run if the USB serial port is closed and reopened,
    causing time_us_64() to restart from zero. This creates a sudden large jump
    in raw latency values (t1_ns - t0_ns) at the reset boundary. The correction
    detects this jump, splits the data into epochs at the reset point, and
    applies independent drift correction to each epoch before recombining.

    Within each epoch:
    1. Fit a linear regression over sequence number to measure drift rate.
    2. Subtract the linear trend.
    3. Subtract the median of the first 12 events of the epoch to centre
       values around zero, exposing only the true OS jitter.

    If no reset is detected the data is treated as a single epoch.
    """
    df = df.copy().sort_values("seq").reset_index(drop=True)
    raw = df["t_latency_ns"].astype(np.int64)
    df["t_latency_ns_raw"] = raw

    # ---- Detect Pico mid-run reset ------------------------------------------
    # A reset appears as a sudden large negative jump in consecutive raw values.
    # Threshold: any jump larger than 10 seconds is certainly a reset, not drift.
    RESET_THRESHOLD_NS = 10_000_000_000   # 10 seconds
    diffs = raw.diff().abs()
    reset_candidates = diffs[diffs > RESET_THRESHOLD_NS]

    if len(reset_candidates) > 0:
        reset_idx  = reset_candidates.index[0]
        reset_seq  = int(df.loc[reset_idx, "seq"])
        reset_cycle = int(df.loc[reset_idx, "cycle"])
        print(f"[WARN] Pico mid-run reset detected at seq={reset_seq}, "
              f"cycle={reset_cycle}.")
        print(f"       Raw latency jumped by "
              f"{diffs.loc[reset_idx]/1e9:.1f}s at this point.")
        print(f"       Applying independent drift correction to each epoch.")
        epochs = [
            df[df["seq"] < reset_seq].copy(),
            df[df["seq"] >= reset_seq].copy(),
        ]
    else:
        epochs = [df.copy()]

    # ---- Correct each epoch independently -----------------------------------
    def correct_single_epoch(edf, epoch_num):
        raw_e   = edf["t_latency_ns"].astype(np.float64).values
        seq_e   = edf["seq"].astype(np.float64).values

        slope, intercept = np.polyfit(seq_e, raw_e, 1)
        trend    = slope * seq_e + intercept
        residual = raw_e - trend

        # Centre on first 12 events of this epoch
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

    clean = (pd.concat(clean_parts).sort_values("seq")
               .reset_index(drop=True))
    if outlier_parts:
        outliers = (pd.concat(outlier_parts).sort_values("seq")
                      .reset_index(drop=True))
        if len(outliers) > 0:
            print(f"[INFO] {len(outliers)} outlier(s) removed (>{sigma}σ). "
                  f"See outliers.csv")
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
            "mean_ms":   round(grp.mean(),         4),
            "median_ms": round(grp.median(),        4),
            "std_ms":    round(grp.std(),           4),
            "min_ms":    round(grp.min(),           4),
            "max_ms":    round(grp.max(),           4),
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
    data   = [df[df["char"] == c]["t_latency_ms"].values for c in PATTERN]
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
# Subsample to max 5000 points to keep the plot readable at 12 000 events
# Force y-axis to ±50ms
# ---------------------------------------------------------------------------

def plot_scatter_stability(df: pd.DataFrame,
                           out: str = "scatter_stability.png") -> None:
    plot_df = df
    if len(df) > 5000:
        plot_df = df.sample(n=5000, random_state=42).sort_values("seq")
        print(f"[INFO] Scatter plot: subsampled to 5000 of {len(df)} points "
              f"for readability")

    all_vals = df["t_latency_ms"]
    y_lo = max(-50, all_vals.quantile(0.005) - 2)
    y_hi = min(50,  all_vals.quantile(0.995) + 2)

    fig, ax = plt.subplots(figsize=(13, 4))
    for char in PATTERN:
        grp = plot_df[plot_df["char"] == char]
        ax.scatter(grp["seq"], grp["t_latency_ms"],
                   s=3, alpha=0.35, color=CHAR_COLOR[char], label=char,
                   linewidths=0)

    ax.set_ylim(y_lo, y_hi)
    ax.set_xlabel("Sequence number", fontsize=12)
    ax.set_ylabel("Latency (ms)", fontsize=12)
    ax.set_title("Latency stability over run", fontsize=13)
    ax.legend(title="Key", ncol=4, fontsize=8, markerscale=2)
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

# =============================================================================
# USE CASE 4 — KEYSTROKE DYNAMICS FEATURE EXTRACTION
# =============================================================================
# Computes dwell time and flight time from the raw key logger CSV.
# The raw log contains both DOWN and UP events which are needed for dwell time.
# The generator provides ground truth: PRESS_DURATION_MS dwell, 
# (PRESS_DURATION_MS + PRESS_INTERVAL_MS) flight time DOWN-to-DOWN.
# Any deviation from these values is OS-introduced distortion.
# =============================================================================

def load_raw_for_uc4(path: str) -> pd.DataFrame:
    """
    Load the raw key logger CSV and filter to pattern keys only.
    Strips non-pattern events (Ctrl+C, modifier keys etc) that appear
    at the start or end of the recording session.
    """
    df = pd.read_csv(path, keep_default_na=False,
                     encoding="cp1252", encoding_errors="replace")
    df["keyname"] = clean_keyname(df["keyname"])
    df["edge"]    = df["edge"].str.strip().str.upper()
    df["t1_ns"]   = df["t1_ns"].astype(np.int64)

    # Keep only pattern characters
    pattern_set = set(PATTERN)
    df = df[df["keyname"].isin(pattern_set)].reset_index(drop=True)
    return df


def extract_features(raw: pd.DataFrame,
                     press_ms: int = 30,
                     interval_ms: int = 70) -> pd.DataFrame:
    """
    Compute dwell time and flight time from raw DOWN/UP events.

    Dwell time  = time from DOWN to the next UP for the same key (ms)
    Flight time = time from one DOWN to the next DOWN (ms)

    Ground truth from generator:
        dwell   = press_ms        (30ms)
        flight  = press_ms + interval_ms  (100ms, DOWN-to-DOWN)

    Deviation from ground truth is the OS-introduced distortion.
    """
    rows = []
    down_times = {}   # vkey -> t1_ns of last DOWN

    prev_down_ns = None
    prev_char    = None
    event_idx    = 0

    for _, row in raw.iterrows():
        char = row["keyname"]
        edge = row["edge"]
        t    = int(row["t1_ns"])
        vkey = int(row["vkey"])

        if edge == "DOWN":
            # Flight time: DOWN-to-DOWN from previous key
            if prev_down_ns is not None:
                flight_ms = (t - prev_down_ns) / 1_000_000.0
                rows.append({
                    "event_idx":       event_idx,
                    "char":            char,
                    "from_char":       prev_char,
                    "feature":         "flight_time_ms",
                    "measured_ms":     flight_ms,
                    "expected_ms":     press_ms + interval_ms,
                    "deviation_ms":    flight_ms - (press_ms + interval_ms),
                })
                event_idx += 1

            down_times[vkey] = t
            prev_down_ns = t
            prev_char    = char

        elif edge == "UP":
            # Dwell time: UP minus corresponding DOWN
            if vkey in down_times:
                dwell_ms = (t - down_times[vkey]) / 1_000_000.0
                rows.append({
                    "event_idx":       event_idx,
                    "char":            char,
                    "from_char":       None,
                    "feature":         "dwell_time_ms",
                    "measured_ms":     dwell_ms,
                    "expected_ms":     press_ms,
                    "deviation_ms":    dwell_ms - press_ms,
                })
                event_idx += 1
                del down_times[vkey]

    return pd.DataFrame(rows)


def compute_feature_stats(features: pd.DataFrame) -> pd.DataFrame:
    """Per-key, per-feature descriptive statistics."""
    rows = []
    for feature in ["dwell_time_ms", "flight_time_ms"]:
        fdf = features[features["feature"] == feature]
        for char in PATTERN:
            grp = fdf[fdf["char"] == char]["deviation_ms"]
            if len(grp) == 0:
                continue
            meas = fdf[fdf["char"] == char]["measured_ms"]
            rows.append({
                "feature":        feature,
                "char":           char,
                "count":          len(grp),
                "expected_ms":    fdf[fdf["char"] == char]["expected_ms"].iloc[0],
                "mean_ms":        round(meas.mean(),         3),
                "median_ms":      round(meas.median(),       3),
                "std_ms":         round(meas.std(),          3),
                "deviation_mean": round(grp.mean(),          3),
                "deviation_std":  round(grp.std(),           3),
                "deviation_p5":   round(grp.quantile(0.05),  3),
                "deviation_p95":  round(grp.quantile(0.95),  3),
                "min_ms":         round(meas.min(),          3),
                "max_ms":         round(meas.max(),          3),
            })
    return pd.DataFrame(rows)


def plot_dwell_time(features: pd.DataFrame,
                   press_ms: int = 30,
                   out: str = "uc4_dwell_time.png") -> None:
    """
    Box plot of OS-reported dwell time per key.
    Red dashed line shows the generator ground truth (press_ms).
    Deviation above the line = keyboard controller added latency to UP event.
    """
    dwell = features[features["feature"] == "dwell_time_ms"]
    fig, ax = plt.subplots(figsize=(13, 5))

    data   = [dwell[dwell["char"] == c]["measured_ms"].values for c in PATTERN]
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

    # Ground truth line
    ax.axhline(press_ms, color="red", linewidth=1.2, linestyle="--",
               label=f"Generator ground truth ({press_ms}ms)")

    ax.set_xticks(range(1, len(PATTERN) + 1))
    ax.set_xticklabels(PATTERN)
    ax.set_xlabel("Key", fontsize=12)
    ax.set_ylabel("OS-reported dwell time (ms)", fontsize=12)
    ax.set_title("UC4 — Dwell time per key vs generator ground truth", fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_flight_time(features: pd.DataFrame,
                    press_ms: int = 30,
                    interval_ms: int = 70,
                    out: str = "uc4_flight_time.png") -> None:
    """
    Box plot of OS-reported flight time (DOWN-to-DOWN) per key.
    Red dashed line shows generator ground truth (press_ms + interval_ms).
    """
    flight = features[features["feature"] == "flight_time_ms"]
    ground_truth = press_ms + interval_ms

    fig, ax = plt.subplots(figsize=(13, 5))
    data   = [flight[flight["char"] == c]["measured_ms"].values for c in PATTERN]
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

    ax.axhline(ground_truth, color="red", linewidth=1.2, linestyle="--",
               label=f"Generator ground truth ({ground_truth}ms)")

    ax.set_xticks(range(1, len(PATTERN) + 1))
    ax.set_xticklabels(PATTERN)
    ax.set_xlabel("Key", fontsize=12)
    ax.set_ylabel("OS-reported flight time DOWN→DOWN (ms)", fontsize=12)
    ax.set_title("UC4 — Flight time per key vs generator ground truth", fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_deviation_overlay(features: pd.DataFrame,
                           out: str = "uc4_deviation_overlay.png") -> None:
    """
    Side-by-side deviation strips for dwell and flight time.
    Shows how far each OS measurement deviates from ground truth.
    X=0 is perfect. Positive = arrived late / held longer than expected.
    """
    fig, axes = plt.subplots(1, 2, figsize=(16, 5))

    for ax, feature, title in [
        (axes[0], "dwell_time_ms",  "Dwell time deviation from ground truth"),
        (axes[1], "flight_time_ms", "Flight time deviation from ground truth"),
    ]:
        fdf = features[features["feature"] == feature]
        for pos, char in enumerate(PATTERN):
            grp = fdf[fdf["char"] == char]["deviation_ms"]
            if len(grp) == 0:
                continue
            ax.scatter(grp, [pos] * len(grp),
                       s=3, alpha=0.2,
                       color=CHAR_COLOR[char], linewidths=0)
            med = grp.median()
            ax.plot(med, pos, marker="|",
                    color=CHAR_COLOR[char],
                    markersize=14, markeredgewidth=2)

        ax.axvline(0, color="black", linewidth=0.8,
                   linestyle="--", alpha=0.6)
        ax.set_yticks(range(len(PATTERN)))
        ax.set_yticklabels(PATTERN, fontsize=10)
        ax.set_xlabel("Deviation from ground truth (ms)", fontsize=11)
        ax.set_title(title, fontsize=11)
        ax.grid(axis="x", alpha=0.2)

    fig.suptitle("UC4 — OS timing deviation from generator ground truth",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(out, dpi=FIGURE_DPI)
    plt.close(fig)
    print(f"[OUT] {out}")


def plot_dwell_distribution(features: pd.DataFrame,
                            press_ms: int = 30,
                            out: str = "uc4_dwell_distribution.png") -> None:
    """
    Histogram of OS-reported dwell times — all keys overlaid.
    Shows the distribution shape and how far it sits above ground truth.
    """
    dwell = features[features["feature"] == "dwell_time_ms"]
    fig, ax = plt.subplots(figsize=(11, 5))

    all_vals = dwell["measured_ms"]
    xmin = max(0, all_vals.quantile(0.001) - 5)
    xmax = min(150, all_vals.quantile(0.999) + 5)
    bins = np.arange(xmin, xmax + 1, 1)   # 1ms bins for dwell

    for char in PATTERN:
        grp = dwell[dwell["char"] == char]["measured_ms"]
        if len(grp) == 0:
            continue
        ax.hist(grp, bins=bins, alpha=0.4, label=char,
                color=CHAR_COLOR[char], edgecolor="none")

    ax.axvline(press_ms, color="red", linewidth=1.5, linestyle="--",
               label=f"Ground truth ({press_ms}ms)")
    ax.set_xlabel("OS-reported dwell time (ms)", fontsize=12)
    ax.set_ylabel("Count", fontsize=12)
    ax.set_title("UC4 — Dwell time distribution (all keys, 1ms bins)", fontsize=13)
    ax.legend(title="Key", ncol=4, fontsize=9)
    ax.grid(axis="y", alpha=0.3)
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
    parser.add_argument("--serial",  required=True)
    parser.add_argument("--keylog",  required=True)
    parser.add_argument("--raw",     required=False, default=None,
                        help="Raw key logger CSV (*_raw.csv) — activates UC4 "
                             "dwell/flight time feature extraction")
    parser.add_argument("--press-ms",           type=int,   default=30)
    parser.add_argument("--interval-ms",        type=int,   default=70)
    parser.add_argument("--sigma",              type=float, default=3.0)
    parser.add_argument("--max-overlay-cycles", type=int,   default=1000)
    args = parser.parse_args()

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

    print("[...] Aligning sequences...")
    serial, keylog = align_sequences(serial, keylog)
    print(f"      After alignment: serial={len(serial)}, keylog={len(keylog)}")

    print("[...] Correlating...")
    corr  = correlate(serial, keylog)
    valid = corr[corr["valid"]].copy()
    print(f"      {len(valid)} / {len(corr)} events valid after character check")

    if len(valid) == 0:
        print("\n[ERROR] No valid events. Check correlation_errors.csv")
        return

    print("[...] Applying clock offset correction...")
    valid, offset_ns = apply_clock_offset(valid)

    print("[...] Removing outliers...")
    clean, _ = remove_outliers(valid, sigma=args.sigma)
    print(f"      {len(clean)} clean events remain")

    clean.to_csv("correlated.csv", index=False)
    print("[OUT] correlated.csv")

    stats = compute_stats(clean)
    stats.to_csv("stats.csv", index=False)
    print("[OUT] stats.csv")
    print("\n--- Per-key latency statistics (ms) ---")
    print(stats.to_string(index=False))

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

    print("\n[...] Generating standard plots...")
    plot_histogram(clean)
    plot_boxplot_per_key(clean)
    plot_scatter_stability(clean)
    plot_pattern_overlay(clean, args.press_ms, args.interval_ms,
                         args.max_overlay_cycles)
    plot_crossval(cv)

    # ---- UC4: feature extraction from raw log --------------------------------
    if args.raw is not None:
        print(f"\n[UC4] Raw log     : {args.raw}")
        raw_df = load_raw_for_uc4(args.raw)
        print(f"      {len(raw_df)} pattern events loaded "
              f"(DOWN + UP, non-pattern keys stripped)")

        if len(raw_df) < 24:
            print("[UC4] Too few events for feature extraction — skipping.")
        else:
            print("[UC4] Extracting dwell and flight time features...")
            features = extract_features(raw_df, args.press_ms, args.interval_ms)
            features.to_csv("uc4_features.csv", index=False)
            print("[OUT] uc4_features.csv")

            feat_stats = compute_feature_stats(features)
            feat_stats.to_csv("uc4_stats.csv", index=False)
            print("[OUT] uc4_stats.csv")

            # Console summary
            print("\n--- UC4 dwell time statistics (ms) ---")
            dwell_stats = feat_stats[feat_stats["feature"] == "dwell_time_ms"]
            print(dwell_stats[["char","count","expected_ms","mean_ms","std_ms",
                                "deviation_mean","deviation_std",
                                "deviation_p5","deviation_p95"]].to_string(index=False))

            print("\n--- UC4 flight time statistics (ms) ---")
            flight_stats = feat_stats[feat_stats["feature"] == "flight_time_ms"]
            print(flight_stats[["char","count","expected_ms","mean_ms","std_ms",
                                 "deviation_mean","deviation_std",
                                 "deviation_p5","deviation_p95"]].to_string(index=False))

            print("\n[UC4] Generating UC4 plots...")
            plot_dwell_time(features, args.press_ms)
            plot_flight_time(features, args.press_ms, args.interval_ms)
            plot_deviation_overlay(features)
            plot_dwell_distribution(features, args.press_ms)
            print("[UC4] Done.")

    print("\n[DONE]")


if __name__ == "__main__":
    main()