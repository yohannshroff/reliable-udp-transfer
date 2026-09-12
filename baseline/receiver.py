"""Baseline receiver -- Go-Back-N semantics.

* Delivers packets strictly in order.
* Buffers NOTHING out of order: an out-of-order DATA packet is discarded and a
  NAK (plus a duplicate cumulative ACK) is sent for the next expected seq.
* Sends a cumulative ACK carrying ``ack = next_expected_seq`` (i.e. "I have
  everything with seq < ack").
* FIN carries ``seq = <number of data packets>``; it is ACKed only once every
  data packet has been delivered.

Run:
    python -m baseline.receiver --port 5001 --out out.bin \
        --loss 0.05 --seed 1 --log-name baseline_loss5_rx
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import packet as pkt
from common.channel_sim import Channel, make_udp_socket
from common.logger import Logger

RECV_BUF = 65535


def run(args):
    sock = make_udp_socket(("0.0.0.0", args.port))
    chan = Channel(
        sock,
        loss_rate=args.loss,
        delay_mean=args.delay,
        delay_jitter=args.jitter,
        reorder_rate=args.reorder,
        seed=(args.seed + 1000) if args.seed is not None else None,
        name="rx-chan",
    )
    log = Logger(
        args.log_name,
        outdir=args.results_dir,
        meta={
            "role": "receiver",
            "protocol": "baseline-gbn",
            "loss": args.loss,
            "seed": args.seed,
            "out": args.out,
        },
    )

    rc = 2
    expected = 0                 # next in-order seq we still need
    bytes_written = 0
    peer = None
    fout = open(args.out, "wb")
    done = False
    fin_seq = None
    last_rx = time.monotonic()

    print(f"[baseline-rx] listening on :{args.port} -> {args.out} (loss={args.loss})")

    try:
        while not done:
            try:
                sock.settimeout(1.0)
                data, addr = chan.recvfrom(RECV_BUF)
            except (TimeoutError, OSError):
                if peer and (time.monotonic() - last_rx) > args.idle_timeout:
                    print("[baseline-rx] idle timeout, giving up")
                    break
                continue

            last_rx = time.monotonic()
            peer = addr
            p = pkt.decode(data)
            if p is None:
                continue

            if not p.checksum_valid:
                log.event("CORRUPT", seq=p.seq, info="bad checksum")
                # nudge the sender with a duplicate cumulative ACK
                chan.sendto(pkt.encode(0, expected, pkt.ACK), peer)
                continue

            if p.flags & pkt.FIN:
                fin_seq = p.seq
                if expected >= fin_seq:
                    # every data packet delivered -> ack the FIN
                    log.event("FIN", seq=p.seq, info="all data received, acking FIN")
                    chan.sendto(pkt.encode(0, expected + 1, pkt.ACK | pkt.FIN), peer)
                    done = True
                    break
                else:
                    log.event("FIN", seq=p.seq, info=f"FIN early, still need seq>={expected}")
                    chan.sendto(pkt.encode(0, expected, pkt.NAK), peer)
                    chan.sendto(pkt.encode(0, expected, pkt.ACK), peer)
                    continue

            if not (p.flags & pkt.DATA):
                continue

            if p.seq == expected:
                fout.write(p.payload)
                bytes_written += len(p.payload)
                expected += 1
                log.on_bytes_acked(len(p.payload))
                log.event("DELIVER", seq=p.seq)
                chan.sendto(pkt.encode(0, expected, pkt.ACK), peer)
            elif p.seq < expected:
                # already have it -> duplicate cumulative ACK
                log.event("DUP", seq=p.seq)
                chan.sendto(pkt.encode(0, expected, pkt.ACK), peer)
            else:
                # gap: GBN discards and NAKs
                log.event("GAP", seq=p.seq, info=f"expected {expected}")
                chan.sendto(pkt.encode(0, expected, pkt.NAK), peer)
                chan.sendto(pkt.encode(0, expected, pkt.ACK), peer)

        # linger briefly to re-ACK retransmitted FINs
        if done and peer:
            linger_end = time.monotonic() + args.linger
            while time.monotonic() < linger_end:
                try:
                    sock.settimeout(0.2)
                    data, addr = chan.recvfrom(RECV_BUF)
                except (TimeoutError, OSError):
                    continue
                p = pkt.decode(data)
                if p and p.checksum_valid and (p.flags & pkt.FIN):
                    chan.sendto(pkt.encode(0, expected + 1, pkt.ACK | pkt.FIN), addr)
    finally:
        fout.close()
        summary = log.finish(
            extra={
                "bytes_written": bytes_written,
                "packets_delivered": expected,
                "channel": chan.stats(),
                "completed": done,
            }
        )
        log.close()
        chan.close()
        print(f"[baseline-rx] wrote {bytes_written} B in {expected} packets, completed={done}")
        print(f"[baseline-rx] channel: {chan.stats()}")
        print(f"[baseline-rx] summary -> {log.path_summary}")
        rc = 0 if done else 2
    return rc


def build_parser():
    ap = argparse.ArgumentParser(description="Baseline (GBN) reliable-UDP receiver")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--out", required=True, help="output file path")
    ap.add_argument("--loss", type=float, default=0.0, help="ACK-path loss rate 0..1")
    ap.add_argument("--delay", type=float, default=0.0, help="one-way delay seconds")
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--linger", type=float, default=2.0)
    ap.add_argument("--idle-timeout", type=float, default=20.0)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--log-name", default="baseline_rx")
    return ap


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
