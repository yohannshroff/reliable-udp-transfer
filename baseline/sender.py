"""Baseline sender -- Go-Back-N ARQ, FIXED retransmission timeout, DAIMD.

* One retransmission timer, on the oldest unacknowledged packet.
* On timeout OR on a NAK, the ENTIRE outstanding window is retransmitted
  starting at ``base`` (classic Go-Back-N).
* RTO is a fixed constant (``--rto``, default 200 ms) -- it does NOT adapt.
* Congestion control is the shared DAIMD controller from ``common/`` -- the
  same code the improved sender uses, so the comparison isolates ARQ + RTO.

Run:
    python -m baseline.sender --host 127.0.0.1 --port 5001 \
        --file test_files/medium.bin --loss 0.05 --seed 1 \
        --log-name baseline_loss5
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import packet as pkt
from common.channel_sim import Channel, make_udp_socket
from common.congestion import DAIMD, LossEpochGate
from common.logger import Logger

RECV_BUF = 65535


def chunk_file(path, size):
    chunks = []
    with open(path, "rb") as f:
        while True:
            b = f.read(size)
            if not b:
                break
            chunks.append(b)
    return chunks


def run(args):
    chunks = chunk_file(args.file, pkt.MAX_PAYLOAD)
    N = len(chunks)
    total_bytes = sum(len(c) for c in chunks)
    if N == 0:
        chunks = [b""]
        N = 1

    sock = make_udp_socket()
    chan = Channel(
        sock,
        loss_rate=args.loss,
        delay_mean=args.delay,
        delay_jitter=args.jitter,
        reorder_rate=args.reorder,
        seed=args.seed,
        burst_at=args.burst_at,
        burst_len=args.burst_len,
        name="tx-chan",
    )
    log = Logger(
        args.log_name,
        outdir=args.results_dir,
        meta={
            "role": "sender",
            "protocol": "baseline-gbn",
            "arq": "go-back-n",
            "rto_mode": "fixed",
            "rto_ms": args.rto * 1000.0,
            "loss": args.loss,
            "seed": args.seed,
            "file": args.file,
            "file_bytes": total_bytes,
            "n_packets": N,
            "mss": pkt.MAX_PAYLOAD,
            "delay": args.delay,
            "jitter": args.jitter,
            "burst_at": args.burst_at,
            "burst_len": args.burst_len,
        },
    )

    peer = (args.host, args.port)
    rto = args.rto
    # Shared DAIMD + shared loss-epoch gate, identical to the improved sender
    # -- see common/congestion.py.
    cc = DAIMD(init_cwnd=args.init_cwnd)
    gate = LossEpochGate()

    rc = 2
    lock = threading.Lock()
    state = {
        "base": 0,          # oldest unacked seq
        "next_seq": 0,      # next seq to transmit
        "highest_sent": -1, # for RETX vs SEND classification
        "timer_start": None,
        "fin_acked": False,
        "loss_events": 0,
        "last_gbn_base": -1,
        "last_gbn_t": -1e9,
        "stop": False,
    }

    def cum_bytes(lo, hi):
        return sum(len(chunks[i]) for i in range(lo, hi) if i < N)

    def send_packet(seq):
        is_retx = seq <= state["highest_sent"]
        chan.sendto(pkt.encode(seq, 0, pkt.DATA, chunks[seq]), peer)
        if seq > state["highest_sent"]:
            state["highest_sent"] = seq
        log.event(
            "RETX" if is_retx else "SEND",
            seq=seq,
            cwnd=cc.cwnd,
            rto_ms=rto * 1000.0,
        )

    def go_back_n(reason):
        """Retransmit the whole outstanding window from base.

        A NAK-triggered Go-Back-N is rate-limited to once per RTO per window
        base: while one full-window resend is already in flight, further
        duplicate NAKs for the same gap are ignored (classic GBN: one "go
        back" per round trip). A fixed-RTO timeout always forces the resend --
        that timer is the backstop when the resend itself is lost.
        """
        if state["base"] >= N:
            return
        now = time.monotonic()
        if reason == "nak":
            if (state["base"] == state["last_gbn_base"]
                    and now - state["last_gbn_t"] < rto):
                return
        state["last_gbn_base"] = state["base"]
        state["last_gbn_t"] = now
        state["loss_events"] += 1
        if gate.should_cut(state["base"], state["highest_sent"]):
            cc.on_loss()
        log.event("TIMEOUT" if reason == "timeout" else "FASTRETX",
                  seq=state["base"], cwnd=cc.cwnd, rto_ms=rto * 1000.0, info=reason)
        state["next_seq"] = state["base"]
        state["timer_start"] = now

    # -- ACK/NAK listener ------------------------------------------------
    def listener():
        while not state["stop"]:
            try:
                sock.settimeout(0.5)
                data, _ = chan.recvfrom(RECV_BUF)
            except (TimeoutError, OSError):
                continue
            p = pkt.decode(data)
            if p is None or not p.checksum_valid:
                continue

            with lock:
                if (p.flags & pkt.ACK) and (p.flags & pkt.FIN):
                    state["fin_acked"] = True
                    log.event("ACK", seq=-1, cwnd=cc.cwnd, info="FIN acked")
                    continue

                if p.flags & pkt.ACK:
                    n = p.ack  # everything with seq < n is acknowledged
                    if n > state["base"]:
                        newly = cum_bytes(state["base"], min(n, N))
                        n_pkts = min(n, N) - state["base"]
                        for _ in range(max(0, n_pkts)):
                            cc.on_ack()
                        log.on_bytes_acked(newly)
                        state["base"] = n
                        log.event("ACK", seq=n, cwnd=cc.cwnd,
                                  info=f"cum ack, base->{n}")
                        if state["base"] < N:
                            state["timer_start"] = time.monotonic()
                        else:
                            state["timer_start"] = None
                    else:
                        log.event("DUPACK", seq=n, cwnd=cc.cwnd)

                elif p.flags & pkt.NAK:
                    log.event("NAK", seq=p.ack, cwnd=cc.cwnd)
                    go_back_n("nak")

    lt = threading.Thread(target=listener, daemon=True)
    lt.start()

    # -- main send loop -----------------------------------------------
    print(f"[baseline-tx] {args.file}: {total_bytes} B in {N} pkts -> {peer} "
          f"(loss={args.loss}, fixed RTO={rto*1000:.0f}ms)")
    t_start = time.monotonic()
    last_stats = 0.0
    try:
        while True:
            with lock:
                if state["base"] >= N:
                    break
                if time.monotonic() - t_start > args.run_timeout:
                    log.event("ABORT", info="run_timeout exceeded")
                    print("[baseline-tx] run timeout exceeded, aborting")
                    break

                window = cc.window
                while state["next_seq"] < state["base"] + window and state["next_seq"] < N:
                    send_packet(state["next_seq"])
                    if state["timer_start"] is None:
                        state["timer_start"] = time.monotonic()
                    state["next_seq"] += 1

                if (state["timer_start"] is not None
                        and time.monotonic() - state["timer_start"] > rto):
                    go_back_n("timeout")

                now = time.monotonic()
                if now - last_stats >= args.stats_interval:
                    log.stats_sample(cwnd=cc.cwnd, rto_ms=rto * 1000.0,
                                     info=f"base={state['base']} next={state['next_seq']}")
                    last_stats = now

            # light pacing: ~ cwnd packets per RTO worth of time
            time.sleep(min(0.002, rto / max(1, cc.window) / 4))

        # -- FIN handshake ------------------------------------------
        if state["base"] >= N:
            for attempt in range(args.fin_retries):
                if state["fin_acked"]:
                    break
                chan.sendto(pkt.encode(N, 0, pkt.FIN), peer)
                log.event("FIN", seq=N, info=f"attempt {attempt+1}")
                deadline = time.monotonic() + rto
                while time.monotonic() < deadline and not state["fin_acked"]:
                    time.sleep(0.02)
    finally:
        state["stop"] = True
        lt.join(timeout=1.0)
        completed = state["base"] >= N and state["fin_acked"]
        summary = log.finish(
            extra={
                "completed": completed,
                "fin_acked": state["fin_acked"],
                "loss_events": state["loss_events"],
                "final_cwnd": round(cc.cwnd, 3),
                "cc_increases": cc.n_increase,
                "cc_decreases": cc.n_decrease,
                "channel": chan.stats(),
            }
        )
        log.close()
        chan.close()
        dur = summary["transfer_duration_s"]
        print(f"[baseline-tx] done completed={completed} "
              f"dur={dur:.3f}s thr={summary['throughput_Mbps']:.3f} Mbps "
              f"retx={summary['retransmissions']} "
              f"overhead={summary['retransmission_overhead_pct']}%")
        print(f"[baseline-tx] channel: {chan.stats()}")
        print(f"[baseline-tx] summary -> {log.path_summary}")
        rc = 0 if completed else 2
    return rc


def build_parser():
    ap = argparse.ArgumentParser(description="Baseline (GBN + fixed RTO + DAIMD) sender")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--file", required=True)
    ap.add_argument("--loss", type=float, default=0.0, help="data-path loss rate 0..1")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--rto", type=float, default=0.2, help="FIXED retransmission timeout, seconds")
    ap.add_argument("--init-cwnd", type=float, default=4.0)
    ap.add_argument("--burst-at", type=float, default=None, help="start a total-loss burst N s in")
    ap.add_argument("--burst-len", type=float, default=0.0)
    ap.add_argument("--stats-interval", type=float, default=0.1)
    ap.add_argument("--fin-retries", type=int, default=12)
    ap.add_argument("--run-timeout", type=float, default=120.0)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--log-name", default="baseline_tx")
    return ap


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
