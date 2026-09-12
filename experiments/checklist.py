"""Validation checklist (project spec Section 9). Exits non-zero on any failure.

    .venv/bin/python -m experiments.checklist        (or plain python3)

Checks, for both protocols:
  * file integrity (SHA-256) at 0 / 1 / 5 / 10 % loss on tiny, small, medium, large
  * 0 % loss => zero retransmissions
  * sender + receiver both terminate (no hang) even at high loss
  * channel's empirical drop rate ~= configured rate
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from experiments.run_transfer import run_once  # noqa: E402
from common.channel_sim import Channel, make_udp_socket  # noqa: E402

FILES = ["tiny.bin", "small.bin", "medium.bin", "large.bin"]
LOSSES = [0.0, 0.01, 0.05, 0.10]
RESULTS = "results/checklist"


def check_channel_drop_rate():
    print("\n[1] channel empirical drop rate")
    ok = True
    for rate in (0.01, 0.05, 0.10, 0.30):
        s = make_udp_socket(("127.0.0.1", 0))
        d = make_udp_socket(("127.0.0.1", 0))
        ch = Channel(s, loss_rate=rate, seed=123, name="c")
        n = 20000
        for _ in range(n):
            ch.sendto(b"x", d.getsockname())
        emp = ch.empirical_loss_rate()
        ch.close(); d.close()
        good = abs(emp - rate) <= max(0.01, 0.15 * rate)
        ok &= good
        print(f"    configured {rate:>5.2f}  empirical {emp:6.4f}  {'OK' if good else 'FAIL'}")
    return ok


def check_transfers():
    print("\n[2] transfers: integrity + termination + 0%-loss no-retx")
    os.makedirs(RESULTS, exist_ok=True)
    port = 5800
    ok = True
    for proto in ("baseline", "improved"):
        for fname in FILES:
            for loss in LOSSES:
                port += 2
                tag = f"{proto}_{fname.split('.')[0]}_l{int(loss*100)}"
                t0 = time.time()
                res = run_once(
                    proto, f"test_files/{fname}", loss, seed=7, port=port,
                    results_dir=RESULTS, tag=tag, delay=0.01, jitter=0.003,
                    run_timeout=90.0, quiet=True,
                )
                wall = time.time() - t0
                s = json.load(open(res["summary_tx"]))
                integrity = res["integrity_ok"]
                completed = s.get("completed")
                terminated = res["tx_rc"] == 0 and res["rx_rc"] == 0
                no_retx_ok = True
                if loss == 0.0:
                    no_retx_ok = s["retransmissions"] == 0
                row_ok = integrity and completed and terminated and no_retx_ok
                ok &= row_ok
                flags = []
                if not integrity: flags.append("INTEGRITY")
                if not completed: flags.append("INCOMPLETE")
                if not terminated: flags.append("BAD_RC")
                if not no_retx_ok: flags.append("RETX@0%")
                print(f"    {proto:8} {fname:10} loss={loss:<4} "
                      f"{wall:5.1f}s  {'OK' if row_ok else ' '.join(flags)}")
    return ok


def main():
    a = check_channel_drop_rate()
    b = check_transfers()
    print("\n" + "=" * 50)
    print("CHECKLIST:", "ALL PASS" if (a and b) else "FAILURES ABOVE")
    sys.exit(0 if (a and b) else 1)


if __name__ == "__main__":
    main()
