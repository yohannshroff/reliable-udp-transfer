"""Improved receiver -- Selective Repeat semantics.

* Buffers out-of-order DATA packets (up to seq < deliver_base + window).
* Sends a per-packet SELECTIVE ACK (``ack = seq`` of the packet received).
* Delivers to the output file in order as gaps fill.
* On a detected gap, emits a NAK for each missing seq between the delivery
  cursor and the highest seq seen (rate-limited so we don't NAK-storm).
* FIN carries ``seq = <number of data packets>``; ACKed only once every data
  packet has been delivered, otherwise NAKed for the holes.

Run:
    python -m improved.receiver --port 5001 --out out.bin --loss 0.05 --seed 1 \
        --log-name improved_loss5_rx
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
            "protocol": "improved-sr",
            "loss": args.loss,
            "seed": args.seed,
            "out": args.out,
        },
    )

    deliver_base = 0             # next seq to write to the file
    buf = {}                     # seq -> payload (out-of-order hold)
    max_seen = -1
    bytes_written = 0
    peer = None
    fout = open(args.out, "wb")
    done = False
    last_rx = time.monotonic()
    last_nak = {}               # seq -> last time we NAKed it
    rc = 2

    print(f"[improved-rx] listening on :{args.port} -> {args.out} (loss={args.loss})")

    def maybe_nak(now):
        if max_seen < deliver_base:
            return
        for s in range(deliver_base, max_seen):
            if s in buf:
                continue
            if now - last_nak.get(s, 0.0) >= args.nak_interval:
                chan.sendto(pkt.encode(0, s, pkt.NAK), peer)
                last_nak[s] = now
                log.event("NAK", seq=s)

    try:
        while not done:
            try:
                sock.settimeout(args.nak_interval)
                data, addr = chan.recvfrom(RECV_BUF)
            except (TimeoutError, OSError):
                # proactive NAK: the sender may have finished its initial burst
                # and be sitting on a per-packet timer for a straggler. Keep
                # reminding it about the holes so recovery is NAK-driven, not
                # timeout-driven.
                if peer:
                    maybe_nak(time.monotonic())
                if peer and (time.monotonic() - last_rx) > args.idle_timeout:
                    print("[improved-rx] idle timeout, giving up")
                    break
                continue

            now = time.monotonic()
            last_rx = now
            peer = addr
            p = pkt.decode(data)
            if p is None:
                continue
            if not p.checksum_valid:
                log.event("CORRUPT", seq=p.seq, info="bad checksum")
                continue

            if p.flags & pkt.FIN:
                fin_seq = p.seq
                if deliver_base >= fin_seq:
                    log.event("FIN", seq=p.seq, info="all data delivered, acking FIN")
                    chan.sendto(pkt.encode(0, fin_seq, pkt.ACK | pkt.FIN), peer)
                    done = True
                    break
                log.event("FIN", seq=p.seq, info=f"FIN early, need up to {fin_seq}")
                max_seen = max(max_seen, fin_seq - 1)
                maybe_nak(now)
                continue

            if not (p.flags & pkt.DATA):
                continue

            max_seen = max(max_seen, p.seq)

            # selective ACK for this specific packet (even duplicates)
            chan.sendto(pkt.encode(0, p.seq, pkt.ACK), peer)

            if p.seq < deliver_base:
                log.event("DUP", seq=p.seq)
            elif p.seq in buf:
                log.event("DUP", seq=p.seq, info="already buffered")
            elif p.seq >= deliver_base + args.rwnd:
                # outside receive window -- drop, still ACKed above
                log.event("OOW", seq=p.seq, info=f"deliver_base={deliver_base}")
            else:
                buf[p.seq] = p.payload
                log.event("BUFFER", seq=p.seq, info=f"held={len(buf)}")
                while deliver_base in buf:
                    payload = buf.pop(deliver_base)
                    fout.write(payload)
                    bytes_written += len(payload)
                    log.on_bytes_acked(len(payload))
                    log.event("DELIVER", seq=deliver_base)
                    deliver_base += 1

            maybe_nak(now)

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
                    chan.sendto(pkt.encode(0, p.seq, pkt.ACK | pkt.FIN), addr)
    finally:
        fout.close()
        log.finish(
            extra={
                "bytes_written": bytes_written,
                "packets_delivered": deliver_base,
                "still_buffered_at_exit": len(buf),
                "channel": chan.stats(),
                "completed": done,
            }
        )
        log.close()
        chan.close()
        print(f"[improved-rx] wrote {bytes_written} B in {deliver_base} packets, completed={done}")
        print(f"[improved-rx] channel: {chan.stats()}")
        print(f"[improved-rx] summary -> {log.path_summary}")
        rc = 0 if done else 2
    return rc


def build_parser():
    ap = argparse.ArgumentParser(description="Improved (Selective Repeat) reliable-UDP receiver")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--out", required=True)
    ap.add_argument("--loss", type=float, default=0.0, help="ACK-path loss rate 0..1")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--reorder", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--rwnd", type=int, default=4096, help="receive-buffer window in packets")
    ap.add_argument("--nak-interval", type=float, default=0.05,
                    help="min seconds between repeat NAKs for the same seq")
    ap.add_argument("--linger", type=float, default=2.0)
    ap.add_argument("--idle-timeout", type=float, default=20.0)
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--log-name", default="improved_rx")
    return ap


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
