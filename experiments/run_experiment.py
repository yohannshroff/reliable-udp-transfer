"""Drive baseline vs improved across loss rates (and an optional loss burst),
repeating each configuration for averaging. Writes:

* per-run event CSV + summary JSON  (from the sender/receiver themselves)
* results/experiment_index.csv       (one row per run, the key metrics)
* results/experiment_summary.csv     (mean/std per proto x loss)

Example:
    python -m experiments.run_experiment --file test_files/medium.bin \
        --loss 0 0.01 0.05 0.10 --repeats 3 --delay 0.02 --jitter 0.005
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.run_transfer import run_once

METRIC_KEYS = [
    "transfer_duration_s",
    "throughput_Mbps",
    "retransmissions",
    "total_packets_sent",
    "retransmission_overhead_pct",
    "timeouts",
    "naks_received",
    "loss_events",
    "cc_decreases",
    "final_cwnd",
]


def load_summary(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="test_files/medium.bin")
    ap.add_argument("--loss", type=float, nargs="+", default=[0.0, 0.01, 0.05, 0.10])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--protos", nargs="+", default=["baseline", "improved"])
    ap.add_argument("--delay", type=float, default=0.02)
    ap.add_argument("--jitter", type=float, default=0.005)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--rto", type=float, default=0.2, help="baseline fixed RTO, s")
    ap.add_argument("--burst-at", type=float, default=None)
    ap.add_argument("--burst-len", type=float, default=0.0)
    ap.add_argument("--base-port", type=int, default=5300)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--index-name", default=None,
                    help="per-run CSV name; defaults to experiment_index.csv, "
                         "or experiment_burst_index.csv when --burst-at is set")
    ap.add_argument("--run-timeout", type=float, default=180.0)
    args = ap.parse_args()

    os.makedirs(args.results_dir, exist_ok=True)
    default_name = ("experiment_burst_index.csv" if args.burst_at is not None
                    else "experiment_index.csv")
    index_path = os.path.join(args.results_dir, args.index_name or default_name)
    rows = []
    port = args.base_port

    burst_tag = "_burst" if args.burst_at is not None else ""
    t0 = time.time()
    for loss in args.loss:
        for rep in range(args.repeats):
            seed = 100 + rep  # SAME seed for both protos at a given (loss, rep)
            for proto in args.protos:
                port += 2
                losspct = f"{loss*100:g}"
                tag = f"{proto}_loss{losspct}{burst_tag}_r{rep}"
                print(f"[exp] {tag} seed={seed} port={port} ...", flush=True)
                res = run_once(
                    proto, args.file, loss, seed, port, args.results_dir, tag,
                    delay=args.delay, jitter=args.jitter, reorder=args.reorder,
                    rto=args.rto, burst_at=args.burst_at, burst_len=args.burst_len,
                    run_timeout=args.run_timeout, quiet=True,
                )
                s = load_summary(res["summary_tx"])
                row = {
                    "proto": proto, "loss": loss, "rep": rep, "seed": seed,
                    "integrity_ok": res["integrity_ok"],
                    "completed": s.get("completed"),
                    **{k: s.get(k) for k in METRIC_KEYS},
                    "tag": tag,
                }
                rows.append(row)
                print(f"      dur={row['transfer_duration_s']:.3f}s "
                      f"thr={row['throughput_Mbps']:.3f}Mbps "
                      f"overhead={row['retransmission_overhead_pct']}% "
                      f"integrity={row['integrity_ok']}")

    with open(index_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # aggregate mean/std per (proto, loss)
    agg_path = os.path.join(
        args.results_dir,
        os.path.basename(index_path).replace("index", "summary"),
    )
    agg_keys = ["throughput_Mbps", "retransmission_overhead_pct",
                "transfer_duration_s", "retransmissions", "timeouts"]
    with open(agg_path, "w", newline="") as f:
        cols = ["proto", "loss", "n"]
        for k in agg_keys:
            cols += [k + "_mean", k + "_std"]
        w = csv.writer(f)
        w.writerow(cols)
        seen = sorted({(r["proto"], r["loss"]) for r in rows})
        for proto, loss in seen:
            grp = [r for r in rows if r["proto"] == proto and r["loss"] == loss]
            out = [proto, loss, len(grp)]
            for k in agg_keys:
                vals = [float(r[k]) for r in grp if r[k] is not None]
                out.append(round(statistics.mean(vals), 4) if vals else "")
                out.append(round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0)
            w.writerow(out)

    n_fail = sum(1 for r in rows if not r["integrity_ok"])
    print(f"\n[exp] {len(rows)} runs in {time.time()-t0:.1f}s, "
          f"{n_fail} integrity failures")
    print(f"[exp] per-run   -> {index_path}")
    print(f"[exp] aggregate -> {agg_path}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
