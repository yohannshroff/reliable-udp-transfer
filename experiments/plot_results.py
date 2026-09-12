"""Generate the Review-2 / Review-3 comparison graphs from logged CSVs.

Reads:
    results/experiment_index.csv     (from run_experiment.py)
    results/<tag>.csv                (per-run event logs, for cwnd-over-time)
    results/<...burst...>.csv        (optional, for recovery-time plot)

Writes PNGs into results/:
    fig_throughput_vs_loss.png
    fig_overhead_vs_loss.png
    fig_cwnd_over_time.png
    fig_recovery_after_burst.png     (only if a burst experiment was run)
    fig_throughput_variance.png

Run with the project venv so matplotlib is available:
    .venv/bin/python -m experiments.plot_results
"""
import argparse
import csv
import glob
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE_C = "#c1440e"   # baseline colour
IMPR_C = "#1f6f8b"   # improved colour


def read_index(paths):
    rows = []
    for path in paths:
        with open(path) as f:
            rows.extend(csv.DictReader(f))
    for r in rows:
        r["loss"] = float(r["loss"])
        for k in ("throughput_Mbps", "retransmission_overhead_pct",
                  "transfer_duration_s", "retransmissions", "timeouts",
                  "loss_events", "cc_decreases"):
            r[k] = float(r[k]) if r.get(k) not in (None, "", "None") else float("nan")
    return rows


def group_stats(rows, proto, ykey):
    losses = sorted({r["loss"] for r in rows if r["proto"] == proto})
    xs, means, stds = [], [], []
    for L in losses:
        vals = [r[ykey] for r in rows
                if r["proto"] == proto and r["loss"] == L and r[ykey] == r[ykey]]
        if not vals:
            continue
        xs.append(L * 100)
        means.append(statistics.mean(vals))
        stds.append(statistics.pstdev(vals) if len(vals) > 1 else 0.0)
    return xs, means, stds


def plot_throughput_vs_loss(rows, outdir):
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for proto, c in (("baseline", BASE_C), ("improved", IMPR_C)):
        xs, m, s = group_stats(rows, proto, "throughput_Mbps")
        ax.errorbar(xs, m, yerr=s, marker="o", capsize=3, color=c, label=proto)
    ax.set_xlabel("packet loss rate (%)")
    ax.set_ylabel("throughput (Mbps)")
    ax.set_title("Throughput vs. loss rate")
    ax.grid(True, alpha=0.3)
    ax.legend()
    p = os.path.join(outdir, "fig_throughput_vs_loss.png")
    fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return p


def plot_overhead_vs_loss(rows, outdir):
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    losses = sorted({r["loss"] for r in rows})
    x = range(len(losses))
    w = 0.38
    for i, (proto, c) in enumerate((("baseline", BASE_C), ("improved", IMPR_C))):
        means = []
        for L in losses:
            vals = [r["retransmission_overhead_pct"] for r in rows
                    if r["proto"] == proto and r["loss"] == L]
            means.append(statistics.mean(vals) if vals else 0.0)
        ax.bar([xi + (i - 0.5) * w for xi in x], means, width=w, color=c, label=proto)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{L*100:g}" for L in losses])
    ax.set_xlabel("packet loss rate (%)")
    ax.set_ylabel("retransmission overhead (%)\nretx / total packets sent")
    ax.set_title("Retransmission overhead vs. loss rate")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    p = os.path.join(outdir, "fig_overhead_vs_loss.png")
    fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return p


def _load_events(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return rows


def _representative_run(index_rows, outdir, proto, target_loss):
    cands = [r for r in index_rows
             if r["proto"] == proto and abs(r["loss"] - target_loss) < 1e-9]
    if not cands:
        return None
    cands.sort(key=lambda r: r["throughput_Mbps"])
    tag = cands[len(cands) // 2]["tag"]
    path = os.path.join(outdir, tag + ".csv")
    return path if os.path.exists(path) else None


def plot_cwnd_over_time(index_rows, outdir, target_loss):
    fig, ax = plt.subplots(figsize=(6.8, 4.2))
    plotted = False
    for proto, c in (("baseline", BASE_C), ("improved", IMPR_C)):
        path = _representative_run(index_rows, outdir, proto, target_loss)
        if not path:
            continue
        ev = _load_events(path)
        ts, cw = [], []
        for e in ev:
            if e["event"] == "STATS" and e["cwnd"]:
                ts.append(float(e["t"]))
                cw.append(float(e["cwnd"]))
        if ts:
            ax.plot(ts, cw, color=c, label=proto)
            plotted = True
        # mark retransmission events along the bottom
        rt = [float(e["t"]) for e in ev if e["event"] in ("RETX",)]
        if rt:
            ax.plot(rt, [0.5] * len(rt), "|", color=c, alpha=0.35)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("congestion window (packets)")
    ax.set_title(f"Congestion window over time (loss = {target_loss*100:g}%)")
    ax.grid(True, alpha=0.3)
    if plotted:
        ax.legend()
    p = os.path.join(outdir, "fig_cwnd_over_time.png")
    fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return p


def plot_throughput_variance(rows, outdir):
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for proto, c in (("baseline", BASE_C), ("improved", IMPR_C)):
        xs, m, s = group_stats(rows, proto, "throughput_Mbps")
        cv = [100 * si / mi if mi else 0.0 for si, mi in zip(s, m)]
        ax.plot(xs, cv, marker="s", color=c, label=proto)
    ax.set_xlabel("packet loss rate (%)")
    ax.set_ylabel("throughput coeff. of variation (%)\nacross repeated runs")
    ax.set_title("Throughput stability across repeats")
    ax.grid(True, alpha=0.3)
    ax.legend()
    p = os.path.join(outdir, "fig_throughput_variance.png")
    fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return p


def _goodput_curve(path, bin_s=0.02):
    """(times, bytes_acked) sampled every bin_s from ALL event rows (each row
    carries the running bytes_acked), i.e. far finer than the STATS interval."""
    rows = _load_events(path)
    pts = [(float(e["t"]), float(e["bytes_acked"]))
           for e in rows if e.get("bytes_acked") not in (None, "")]
    if len(pts) < 10:
        return [], []
    T = pts[-1][0]
    ts, bs = [], []
    j = 0
    t = 0.0
    while t <= T:
        while j + 1 < len(pts) and pts[j + 1][0] <= t:
            j += 1
        ts.append(t)
        bs.append(pts[j][1])
        t += bin_s
    return ts, bs


def _recovery_time_from_events(path, burst_at=1.0):
    """Backlog make-up time after the induced loss burst.

    A burst that drops every packet for a while pushes the delivered-bytes
    curve below the trajectory it was on before the burst. "Recovery" here is
    how long, measured from the end of the burst, the flow needs to climb back
    onto that pre-burst trajectory (i.e. erase the backlog the burst created).
    Go-Back-N wastes capacity re-sending correctly-received packets while it
    repairs the gap, so it takes longer to catch up than Selective Repeat.

    pre-burst rate  = slope of delivered bytes over [0.3 s, burst_at]
    target(t)       = bytes(burst_at) + pre_rate * (t - burst_at)
    recovery        = first t > burst_end with bytes(t) >= target(t),
                      minus burst_end
    """
    ts, bs = _goodput_curve(path)
    if not ts or ts[-1] < burst_at + 0.3:
        return None
    def bytes_at(tq):
        lo, hi = 0, len(ts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if ts[mid] < tq:
                lo = mid + 1
            else:
                hi = mid
        return bs[lo]

    pre_idx = [i for i, t in enumerate(ts) if 0.3 <= t <= burst_at]
    if len(pre_idx) < 5:
        return None
    pre_rate = ((bs[pre_idx[-1]] - bs[pre_idx[0]])
                / (ts[pre_idx[-1]] - ts[pre_idx[0]]))
    if pre_rate <= 0:
        return None
    b0 = bytes_at(burst_at)

    # burst end = last sample within 1.5 s of burst_at whose local slope < 15%
    bin_s = ts[1] - ts[0] if len(ts) > 1 else 0.02
    w = max(1, int(0.06 / bin_s))
    burst_end = burst_at
    for i, t in enumerate(ts):
        if not (burst_at <= t <= burst_at + 1.5):
            continue
        a = max(0, i - w)
        loc = (bs[i] - bs[a]) / (ts[i] - ts[a]) if ts[i] > ts[a] else 0.0
        if loc < 0.15 * pre_rate:
            burst_end = t
    for i, t in enumerate(ts):
        if t <= burst_end:
            continue
        target = b0 + pre_rate * (t - burst_at)
        if bs[i] >= target:
            return round(t - burst_end, 4)
    return round(ts[-1] - burst_end, 4)


def _meta_float(outdir, tag, key, default):
    sp = os.path.join(outdir, tag + ".summary.json")
    try:
        import json
        with open(sp) as f:
            return float(json.load(f)["meta"].get(key) or default)
    except Exception:
        return default


def _burst_at_for(outdir, tag):
    return _meta_float(outdir, tag, "burst_at", 1.0)


def _burst_len_for(outdir, tag):
    return _meta_float(outdir, tag, "burst_len", 0.5)


def _rep_burst_run(index_rows, outdir, proto):
    """Pick the median-duration burst run for a protocol."""
    cands = [r for r in index_rows
             if r["proto"] == proto and "burst" in r["tag"]
             and os.path.exists(os.path.join(outdir, r["tag"] + ".csv"))]
    if not cands:
        return None
    cands.sort(key=lambda r: float(r["transfer_duration_s"]))
    return cands[len(cands) // 2]["tag"]


def plot_recovery_after_burst(index_rows, outdir):
    burst_rows = [r for r in index_rows if "burst" in r["tag"]]
    if not burst_rows:
        return None

    if not any(os.path.exists(os.path.join(outdir, r["tag"] + ".csv"))
               for r in burst_rows):
        print("  (burst index present but per-run event CSVs missing -- "
              "re-run the burst experiment; skipping recovery figure)")
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.0, 4.3))

    # -- left: instantaneous goodput timeline around the burst --------
    win = 0.15  # sliding-window width for the rate estimate, seconds
    burst_at = burst_len = None
    drew_left = False
    for proto, c in (("baseline", BASE_C), ("improved", IMPR_C)):
        tag = _rep_burst_run(index_rows, outdir, proto)
        if not tag:
            continue
        drew_left = True
        ba = _burst_at_for(outdir, tag)
        bl = _burst_len_for(outdir, tag)
        burst_at, burst_len = ba, bl
        ts, bs = _goodput_curve(os.path.join(outdir, tag + ".csv"), bin_s=0.02)
        rate_t, rate_v = [], []
        for i, t in enumerate(ts):
            j = i
            while j > 0 and ts[i] - ts[j] < win:
                j -= 1
            if ts[i] > ts[j]:
                rate_t.append(t)
                rate_v.append((bs[i] - bs[j]) / (ts[i] - ts[j]) * 8 / 1e6)
        ax1.plot(rate_t, rate_v, color=c, label=proto, lw=1.4)
    if burst_at is not None:
        ax1.axvspan(burst_at, burst_at + burst_len, color="grey", alpha=0.18,
                    label="loss burst")
    ax1.set_xlabel("time (s)")
    ax1.set_ylabel("instantaneous goodput (Mbps)\n%d ms sliding window" % (win * 1000))
    ax1.set_title("Goodput timeline around a total-loss burst")
    ax1.grid(True, alpha=0.3)
    if drew_left:
        ax1.legend()

    # -- right: cost of the burst -- overhead + duration -------------
    metrics = ("retransmission_overhead_pct", "transfer_duration_s")
    labels = ("retx overhead (%)", "transfer time (s)")
    xs = range(len(metrics))
    w = 0.38
    for k, (proto, c) in enumerate((("baseline", BASE_C), ("improved", IMPR_C))):
        vals = []
        for m in metrics:
            v = [float(r[m]) for r in burst_rows if r["proto"] == proto and r.get(m)]
            vals.append(statistics.mean(v) if v else 0.0)
        ax2.bar([xi + (k - 0.5) * w for xi in xs], vals, width=w, color=c, label=proto)
        for xi, v in zip(xs, vals):
            ax2.text(xi + (k - 0.5) * w, v, f"{v:.1f}", ha="center", va="bottom",
                     fontsize=8)
    ax2.set_xticks(list(xs))
    ax2.set_xticklabels(labels)
    ax2.set_title("Cost of the burst (mean over repeats)")
    ax2.grid(True, axis="y", alpha=0.3)
    ax2.legend()

    p = os.path.join(outdir, "fig_recovery_after_burst.png")
    fig.tight_layout(); fig.savefig(p, dpi=140); plt.close(fig)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--cwnd-loss", type=float, default=0.05,
                    help="loss rate whose representative run is used for the cwnd plot")
    args = ap.parse_args()

    idx_paths = sorted(glob.glob(os.path.join(args.results_dir, "experiment*index.csv")))
    if not idx_paths:
        print(f"no experiment*index.csv in {args.results_dir} -- run "
              f"experiments.run_experiment first")
        sys.exit(1)
    all_rows = read_index(idx_paths)
    # loss-rate sweep plots exclude burst runs (single loss value, would skew)
    sweep_rows = [r for r in all_rows if "burst" not in r["tag"]]

    made = []
    if sweep_rows:
        made += [
            plot_throughput_vs_loss(sweep_rows, args.results_dir),
            plot_overhead_vs_loss(sweep_rows, args.results_dir),
            plot_cwnd_over_time(sweep_rows, args.results_dir, args.cwnd_loss),
            plot_throughput_variance(sweep_rows, args.results_dir),
        ]
    rec = plot_recovery_after_burst(all_rows, args.results_dir)
    if rec:
        made.append(rec)

    for p in made:
        print("wrote", p)


if __name__ == "__main__":
    main()
