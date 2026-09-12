"""Improved sender -- Selective Repeat ARQ, ADAPTIVE RTO, DAIMD.

Differences from the baseline (and ONLY these differences):

* Selective Repeat: each packet is tracked and timed individually; a loss
  (NAK or per-packet timeout) retransmits ONLY that packet, never the window.
* Adaptive RTO: Jacobson/Karels SRTT/RTTVAR estimator (improved/rttest.py),
  Karn's algorithm (no RTT sample from retransmitted packets, exponential
  backoff on timeout).

Congestion control is the SAME shared DAIMD controller as the baseline, with
the same per-epoch decrease gating, so the comparison isolates ARQ + RTO.

Run:
    python -m improved.sender --host 127.0.0.1 --port 5001 \
        --file test_files/medium.bin --loss 0.05 --seed 1 --log-name improved_loss5
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
from improved.rttest import RttEstimator

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
            "protocol": "improved-sr",
            "arq": "selective-repeat",
            "rto_mode": "adaptive",
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
    cc = DAIMD(init_cwnd=args.init_cwnd)
    gate = LossEpochGate()
    rtt = RttEstimator(
        initial_rto=args.init_rto, min_rto=args.min_rto, max_rto=args.max_rto
    )

    lock = threading.Lock()
    acked = set()               # seqs the receiver has confirmed
    send_time = {}              # seq -> monotonic time of most recent (re)transmission
    retx_count = {}             # seq -> number of retransmissions
    dirty = set()               # seqs retransmitted at least once (Karn: no RTT sample)
    inflight = set()            # sent, not yet acked
    state = {
        "base": 0,              # lowest unacked seq
        "next_seq": 0,          # highest seq not yet transmitted once
        "fin_acked": False,
        "loss_events": 0,
        "dupack": 0,            # # of selective ACKs seen for seq > base
        "fr_base": -1,          # base value for which we've already fast-retx'd
        "fast_retx": 0,
        "stop": False,
    }

    def advance_base():
        moved = False
        while state["base"] in acked and state["base"] < N:
            state["base"] += 1
            moved = True
        if moved:
            state["dupack"] = 0
            state["fr_base"] = -1

    def transmit(seq, is_retx):
        chan.sendto(pkt.encode(seq, 0, pkt.DATA, chunks[seq]), peer)
        send_time[seq] = time.monotonic()
        inflight.add(seq)
        if is_retx:
            retx_count[seq] = retx_count.get(seq, 0) + 1
            dirty.add(seq)
            log.event("RETX", seq=seq, cwnd=cc.cwnd,
                      srtt_ms=rtt.srtt_ms, rto_ms=rtt.rto * 1000.0,
                      info=f"retx#{retx_count[seq]}")
        else:
            log.event("SEND", seq=seq, cwnd=cc.cwnd,
                      srtt_ms=rtt.srtt_ms, rto_ms=rtt.rto * 1000.0)

    def note_loss(seq, reason):
        state["loss_events"] += 1
        if gate.should_cut(state["base"], state["next_seq"] - 1):
            cc.on_loss()
        if reason == "timeout":
            rtt.backoff()
            log.event("TIMEOUT", seq=seq, cwnd=cc.cwnd,
                      srtt_ms=rtt.srtt_ms, rto_ms=rtt.rto * 1000.0)

    # -- ACK / NAK listener ------------------------------------------
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
                    seq = p.ack
                    if seq in acked or seq >= N:
                        log.event("DUPACK", seq=seq, cwnd=cc.cwnd)
                        continue
                    acked.add(seq)
                    inflight.discard(seq)
                    now = time.monotonic()
                    if seq not in dirty and seq in send_time:
                        rtt.add_sample(now - send_time[seq])
                    cc.on_ack()
                    log.on_bytes_acked(len(chunks[seq]))
                    was_base = state["base"]
                    advance_base()
                    log.event("ACK", seq=seq, cwnd=cc.cwnd,
                              srtt_ms=rtt.srtt_ms, rto_ms=rtt.rto * 1000.0,
                              info=f"base={state['base']}")

                    # SACK-style fast retransmit: selective ACKs for packets
                    # beyond a still-missing `base` are the Selective-Repeat
                    # equivalent of TCP duplicate ACKs. After `dupthresh` of
                    # them, resend `base` at once instead of waiting for its
                    # per-packet RTO.
                    if state["base"] == was_base and seq > state["base"]:
                        state["dupack"] += 1
                        if (state["dupack"] >= args.fast_retx_dupthresh
                                and state["fr_base"] != state["base"]
                                and state["base"] < N
                                and state["base"] not in acked):
                            state["fr_base"] = state["base"]
                            state["fast_retx"] += 1
                            note_loss(state["base"], "fastretx")
                            transmit(state["base"], is_retx=True)

                elif p.flags & pkt.NAK:
                    seq = p.ack
                    log.event("NAK", seq=seq, cwnd=cc.cwnd)
                    if seq not in acked and seq < N:
                        note_loss(seq, "nak")
                        # retransmit just this packet, unless we sent it very
                        # recently already (NAK + timeout race)
                        last = send_time.get(seq, 0.0)
                        if time.monotonic() - last > args.min_retx_gap:
                            transmit(seq, is_retx=True)

    lt = threading.Thread(target=listener, daemon=True)
    lt.start()

    print(f"[improved-tx] {args.file}: {total_bytes} B in {N} pkts -> {peer} "
          f"(loss={args.loss}, adaptive RTO init={args.init_rto*1000:.0f}ms)")
    t_start = time.monotonic()
    last_stats = 0.0
    rc = 2
    try:
        while True:
            with lock:
                if state["base"] >= N:
                    break
                if time.monotonic() - t_start > args.run_timeout:
                    log.event("ABORT", info="run_timeout exceeded")
                    print("[improved-tx] run timeout exceeded, aborting")
                    break

                window = cc.window

                # (1) keep `window` packets in flight. The window bounds the
                # number of UNACKED packets outstanding, not a raw seq range,
                # so while `base` is being repaired we still push fresh data
                # into the gaps left by already-acked packets ahead of it
                # (Selective Repeat keeps the pipe full during recovery).
                while (len(inflight) < window
                       and state["next_seq"] < N
                       and state["next_seq"] - state["base"] < args.max_ooo):
                    s = state["next_seq"]
                    state["next_seq"] += 1
                    if s not in acked:
                        transmit(s, is_retx=False)

                # (2) per-packet adaptive-RTO timeout sweep
                now = time.monotonic()
                for s in sorted(list(inflight)):
                    if s in acked:
                        continue
                    if now - send_time.get(s, now) > rtt.rto:
                        note_loss(s, "timeout")
                        transmit(s, is_retx=True)

                if now - last_stats >= args.stats_interval:
                    log.stats_sample(
                        cwnd=cc.cwnd, srtt_ms=rtt.srtt_ms, rto_ms=rtt.rto * 1000.0,
                        info=f"base={state['base']} next={state['next_seq']} "
                             f"inflight={len(inflight)}",
                    )
                    last_stats = now

            time.sleep(min(0.002, rtt.rto / max(1, cc.window) / 4))

        # -- FIN handshake ------------------------------------------
        if state["base"] >= N:
            for attempt in range(args.fin_retries):
                if state["fin_acked"]:
                    break
                chan.sendto(pkt.encode(N, 0, pkt.FIN), peer)
                log.event("FIN", seq=N, info=f"attempt {attempt+1}")
                deadline = time.monotonic() + max(rtt.rto, 0.2)
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
                "fast_retransmits": state["fast_retx"],
                "final_cwnd": round(cc.cwnd, 3),
                "cc_increases": cc.n_increase,
                "cc_decreases": cc.n_decrease,
                "rtt_samples": rtt.n_samples,
                "final_srtt_ms": round(rtt.srtt_ms, 3),
                "final_rto_ms": round(rtt.rto * 1000.0, 3),
                "channel": chan.stats(),
            }
        )
        log.close()
        chan.close()
        dur = summary["transfer_duration_s"]
        print(f"[improved-tx] done completed={completed} "
              f"dur={dur:.3f}s thr={summary['throughput_Mbps']:.3f} Mbps "
              f"retx={summary['retransmissions']} "
              f"overhead={summary['retransmission_overhead_pct']}% "
              f"final_rto={summary['final_rto_ms']:.0f}ms")
        print(f"[improved-tx] channel: {chan.stats()}")
        print(f"[improved-tx] summary -> {log.path_summary}")
        rc = 0 if completed else 2
    return rc


def build_parser():
    ap = argparse.ArgumentParser(description="Improved (SR + adaptive RTO + DAIMD) sender")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--file", required=True)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--init-rto", type=float, default=1.0, help="initial RTO before first RTT sample, s")
    ap.add_argument("--min-rto", type=float, default=0.05)
    ap.add_argument("--max-rto", type=float, default=2.0)
    ap.add_argument("--init-cwnd", type=float, default=4.0)
    ap.add_argument("--min-retx-gap", type=float, default=0.02,
                    help="suppress a NAK-triggered retx within this long of the last send")
    ap.add_argument("--fast-retx-dupthresh", type=int, default=3,
                    help="selective ACKs beyond base before a SACK fast retransmit")
    ap.add_argument("--max-ooo", type=int, default=256,
                    help="cap on how far next_seq may run ahead of base (<= receiver rwnd)")
    ap.add_argument("--burst-at", type=float, default=None)
    ap.add_argument("--burst-len", type=float, default=0.0)
    ap.add_argument("--stats-interval", type=float, default=0.1)
    ap.add_argument("--fin-retries", type=int, default=12)
    ap.add_argument("--run-timeout", type=float, default=120.0)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--log-name", default="improved_tx")
    return ap


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
