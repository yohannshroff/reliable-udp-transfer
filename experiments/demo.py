"""Live side-by-side demo for the Review-2 one-to-one.

Runs the baseline and the improved protocol back-to-back over the SAME file at
the SAME loss rate and prints a live progress bar + the headline metrics for
each, then a comparison table.

    .venv/bin/python -m experiments.demo --file test_files/medium.bin --loss 0.08
    .venv/bin/python -m experiments.demo --loss 0.10 --delay 0.02 --jitter 0.005
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from experiments.run_transfer import sha256  # noqa: E402


def _tail_progress(summary_path, label, n_packets, stop_evt):
    """Poll the sender's event CSV and show a crude live progress bar."""
    csv_path = summary_path.replace(".summary.json", ".csv")
    bar_w = 34
    last_pct = -1
    while not stop_evt.is_set():
        acked = 0
        try:
            with open(csv_path) as f:
                for line in f:
                    pass
            # bytes_acked is column index 6
            parts = line.strip().split(",")
            if len(parts) > 6 and parts[6].isdigit():
                acked = int(parts[6])
        except Exception:
            pass
        frac = min(1.0, acked / max(1, n_packets * 1024))
        pct = int(frac * 100)
        if pct != last_pct:
            last_pct = pct
            fill = int(frac * bar_w)
            sys.stdout.write(
                f"\r  {label:9} [{'#' * fill}{'.' * (bar_w - fill)}] {pct:3d}%"
            )
            sys.stdout.flush()
        time.sleep(0.15)
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()


def run_side(proto, args, port):
    results_dir = args.results_dir
    tag = f"demo_{proto}"
    out_path = os.path.join(results_dir, f"{tag}.received.bin")
    if os.path.exists(out_path):
        os.remove(out_path)
    env = dict(os.environ, PYTHONPATH=ROOT)

    rx = subprocess.Popen(
        [sys.executable, "-m", f"{proto}.receiver", "--port", str(port),
         "--out", out_path, "--loss", str(args.loss), "--seed", str(args.seed),
         "--delay", str(args.delay), "--jitter", str(args.jitter),
         "--results-dir", results_dir, "--log-name", f"{tag}_rx"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    tx_cmd = [
        sys.executable, "-m", f"{proto}.sender", "--host", "127.0.0.1",
        "--port", str(port), "--file", args.file, "--loss", str(args.loss),
        "--seed", str(args.seed), "--delay", str(args.delay),
        "--jitter", str(args.jitter), "--results-dir", results_dir,
        "--log-name", tag,
    ]
    if proto == "baseline":
        tx_cmd += ["--rto", str(args.rto)]

    summary_path = os.path.join(results_dir, f"{tag}.summary.json")
    n_packets = max(1, (os.path.getsize(args.file) + 1023) // 1024)
    stop = threading.Event()
    prog = threading.Thread(target=_tail_progress,
                            args=(summary_path, proto, n_packets, stop), daemon=True)
    prog.start()

    t0 = time.time()
    tx = subprocess.Popen(tx_cmd, cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tx.wait()
    try:
        rx.wait(timeout=15)
    except subprocess.TimeoutExpired:
        rx.terminate()
    stop.set()
    prog.join(timeout=1)
    wall = time.time() - t0

    with open(summary_path) as f:
        s = json.load(f)
    ok = os.path.exists(out_path) and sha256(out_path) == sha256(args.file)
    s["_wall_s"] = wall
    s["_integrity_ok"] = ok
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="test_files/medium.bin")
    ap.add_argument("--loss", type=float, default=0.08)
    ap.add_argument("--delay", type=float, default=0.02)
    ap.add_argument("--jitter", type=float, default=0.005)
    ap.add_argument("--rto", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=5701)
    ap.add_argument("--results-dir", default="results")
    args = ap.parse_args()

    size = os.path.getsize(args.file)
    print(f"\n  file        : {args.file}  ({size} B, "
          f"{(size + 1023)//1024} packets)")
    print(f"  loss rate   : {args.loss*100:g}%   "
          f"one-way delay: {args.delay*1000:g}ms +/- {args.jitter*1000:g}ms")
    print(f"  baseline RTO: fixed {args.rto*1000:g}ms      "
          f"improved RTO : adaptive (SRTT/RTTVAR)\n")

    res = {}
    for i, proto in enumerate(("baseline", "improved")):
        print(f"  running {proto} ...")
        res[proto] = run_side(proto, args, args.port + i * 2)

    b, im = res["baseline"], res["improved"]

    def row(name, bval, ival, fmt="{:.3f}", better="low"):
        bs, is_ = fmt.format(bval), fmt.format(ival)
        if better == "low":
            win = "improved" if ival < bval else "baseline"
            delta = (bval - ival) / bval * 100 if bval else 0.0
        else:
            win = "improved" if ival > bval else "baseline"
            delta = (ival - bval) / bval * 100 if bval else 0.0
        print(f"  {name:26} {bs:>12} {is_:>12}   {win:8} ({delta:+.1f}%)")

    print("\n  " + "-" * 72)
    print(f"  {'metric':26} {'baseline':>12} {'improved':>12}   {'winner':8}")
    print("  " + "-" * 72)
    row("transfer time (s)", b["transfer_duration_s"], im["transfer_duration_s"])
    row("throughput (Mbps)", b["throughput_Mbps"], im["throughput_Mbps"],
        better="high")
    row("data packets", b["data_packets_sent"], im["data_packets_sent"], "{:.0f}")
    row("retransmissions", b["retransmissions"], im["retransmissions"], "{:.0f}")
    row("retx overhead (%)", b["retransmission_overhead_pct"],
        im["retransmission_overhead_pct"])
    row("timeouts", b["timeouts"], im["timeouts"], "{:.0f}")
    row("CC decreases", b["cc_decreases"], im["cc_decreases"], "{:.0f}")
    print("  " + "-" * 72)
    print(f"  file integrity (SHA-256)   {str(b['_integrity_ok']):>12} "
          f"{str(im['_integrity_ok']):>12}")
    print("  " + "-" * 72)
    print(f"  improved final adaptive RTO: {im.get('final_rto_ms', 0):.0f} ms "
          f"(SRTT {im.get('final_srtt_ms', 0):.1f} ms)\n")


if __name__ == "__main__":
    main()
