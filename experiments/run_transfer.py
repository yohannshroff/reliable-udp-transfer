"""Run ONE end-to-end file transfer (receiver + sender as subprocesses),
then verify the received file is byte-identical to the source.

Used both as a manual smoke test and as the building block for
run_experiment.py.

Example:
    python -m experiments.run_transfer --proto baseline --file test_files/medium.bin --loss 0.05 --seed 1
    python -m experiments.run_transfer --proto improved --file test_files/medium.bin --loss 0.10 --seed 1
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def run_once(proto, file, loss, seed, port, results_dir, tag,
             delay=0.0, jitter=0.0, reorder=0.0, rto=0.2,
             burst_at=None, burst_len=0.0, run_timeout=120.0, quiet=False):
    rx_mod = f"{proto}.receiver"
    tx_mod = f"{proto}.sender"
    out_path = os.path.join(results_dir, f"{tag}.received.bin")
    os.makedirs(results_dir, exist_ok=True)
    # never let a stale file from a previous run masquerade as success
    if os.path.exists(out_path):
        os.remove(out_path)

    rx_cmd = [
        sys.executable, "-m", rx_mod,
        "--port", str(port), "--out", out_path,
        "--loss", str(loss), "--seed", str(seed),
        "--results-dir", results_dir, "--log-name", f"{tag}_rx",
        "--delay", str(delay), "--jitter", str(jitter), "--reorder", str(reorder),
    ]
    tx_cmd = [
        sys.executable, "-m", tx_mod,
        "--host", "127.0.0.1", "--port", str(port), "--file", file,
        "--loss", str(loss), "--seed", str(seed),
        "--results-dir", results_dir, "--log-name", f"{tag}",
        "--delay", str(delay), "--jitter", str(jitter), "--reorder", str(reorder),
        "--run-timeout", str(run_timeout),
    ]
    if proto == "baseline":
        tx_cmd += ["--rto", str(rto)]
    if burst_at is not None:
        tx_cmd += ["--burst-at", str(burst_at), "--burst-len", str(burst_len)]

    env = dict(os.environ, PYTHONPATH=ROOT)
    out = subprocess.DEVNULL if quiet else None

    rx = subprocess.Popen(rx_cmd, cwd=ROOT, env=env, stdout=out, stderr=out)
    time.sleep(0.6)  # let the receiver bind
    t0 = time.time()
    tx = subprocess.Popen(tx_cmd, cwd=ROOT, env=env, stdout=out, stderr=out)

    tx_rc = tx.wait()
    try:
        rx_rc = rx.wait(timeout=15)
    except subprocess.TimeoutExpired:
        rx.terminate()
        rx_rc = rx.wait()
    wall = time.time() - t0

    src_hash = sha256(file)
    ok_file = os.path.exists(out_path)
    dst_hash = sha256(out_path) if ok_file else None
    integrity = ok_file and (src_hash == dst_hash)

    result = {
        "proto": proto, "file": file, "loss": loss, "seed": seed,
        "tx_rc": tx_rc, "rx_rc": rx_rc, "wall_s": round(wall, 3),
        "integrity_ok": integrity,
        "src_sha256": src_hash[:16], "dst_sha256": (dst_hash[:16] if dst_hash else None),
        "summary_tx": os.path.join(results_dir, f"{tag}.summary.json"),
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", choices=["baseline", "improved"], required=True)
    ap.add_argument("--file", default="test_files/medium.bin")
    ap.add_argument("--loss", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--rto", type=float, default=0.2)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--burst-at", type=float, default=None)
    ap.add_argument("--burst-len", type=float, default=0.0)
    ap.add_argument("--run-timeout", type=float, default=120.0)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    losspct = int(round(args.loss * 100))
    tag = args.tag or f"{args.proto}_loss{losspct}_seed{args.seed}"
    res = run_once(
        args.proto, args.file, args.loss, args.seed, args.port,
        args.results_dir, tag, delay=args.delay, jitter=args.jitter,
        reorder=args.reorder, rto=args.rto, burst_at=args.burst_at,
        burst_len=args.burst_len, run_timeout=args.run_timeout, quiet=args.quiet,
    )
    print()
    for k, v in res.items():
        print(f"  {k:14} {v}")
    print()
    print("  RESULT:", "PASS (byte-identical)" if res["integrity_ok"]
          else "FAIL (mismatch or missing)")
    sys.exit(0 if res["integrity_ok"] else 1)


if __name__ == "__main__":
    main()
