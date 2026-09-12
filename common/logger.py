"""Structured event + metrics logging, identical for both protocol versions.

`plot_results.py` consumes these files without knowing which protocol produced
them, so the schema below must not diverge between baseline and improved.

Two artefacts per run:

* ``<run>.csv``          -- one row per event (SEND / RETX / ACK / NAK / TIMEOUT
                            / DELIVER / STATS / FIN ...)
* ``<run>.summary.json`` -- aggregate metrics computed in ``finish()``

Event CSV columns:
    t            seconds since Logger creation (monotonic)
    event        event type string
    seq          sequence number involved (or -1)
    cwnd         sender congestion window in packets at this instant
    srtt_ms      smoothed RTT estimate in ms (-1 if not tracked / unknown)
    rto_ms       current retransmission timeout in ms
    bytes_acked  cumulative application bytes acknowledged so far
    info         free-form extra text
"""

import csv
import json
import os
import threading
import time

EVENT_FIELDS = [
    "t",
    "event",
    "seq",
    "cwnd",
    "srtt_ms",
    "rto_ms",
    "bytes_acked",
    "info",
]


class Logger:
    def __init__(self, run_name, outdir="results", meta=None, flush_every=200):
        os.makedirs(outdir, exist_ok=True)
        self.run_name = run_name
        self.outdir = outdir
        self.path_events = os.path.join(outdir, run_name + ".csv")
        self.path_summary = os.path.join(outdir, run_name + ".summary.json")
        self.meta = dict(meta or {})

        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._fh = open(self.path_events, "w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(EVENT_FIELDS)
        self._n_since_flush = 0
        self._flush_every = flush_every

        # running aggregates
        self.counts = {}
        self.bytes_acked = 0
        self.first_send_t = None
        self.last_ack_t = None
        # throughput samples: list of (t, bytes_acked) captured on STATS rows
        self.throughput_samples = []

    # -- core -----------------------------------------------------------
    def now(self):
        return time.monotonic() - self._t0

    def event(self, event, seq=-1, cwnd=-1.0, srtt_ms=-1.0, rto_ms=-1.0, info=""):
        t = self.now()
        with self._lock:
            self.counts[event] = self.counts.get(event, 0) + 1
            if event == "SEND" and self.first_send_t is None:
                self.first_send_t = t
            self._writer.writerow(
                [
                    f"{t:.6f}",
                    event,
                    seq,
                    f"{cwnd:.3f}" if cwnd >= 0 else "",
                    f"{srtt_ms:.3f}" if srtt_ms >= 0 else "",
                    f"{rto_ms:.3f}" if rto_ms >= 0 else "",
                    self.bytes_acked,
                    info,
                ]
            )
            self._n_since_flush += 1
            if self._n_since_flush >= self._flush_every:
                self._fh.flush()
                self._n_since_flush = 0

    def on_bytes_acked(self, nbytes):
        """Call whenever new (not duplicate) application bytes are acknowledged."""
        with self._lock:
            self.bytes_acked += nbytes
            self.last_ack_t = self.now()

    def stats_sample(self, cwnd, srtt_ms=-1.0, rto_ms=-1.0, info=""):
        """Periodic snapshot row used for the cwnd-over-time / throughput plots."""
        with self._lock:
            self.throughput_samples.append((self.now(), self.bytes_acked))
        self.event("STATS", seq=-1, cwnd=cwnd, srtt_ms=srtt_ms, rto_ms=rto_ms, info=info)

    # -- teardown ---------------------------------------------------
    def finish(self, extra=None):
        """Flush events and write the summary JSON. Returns the summary dict."""
        with self._lock:
            self._fh.flush()

        sends = self.counts.get("SEND", 0)
        retx = self.counts.get("RETX", 0)
        total_pkts_sent = sends + retx
        duration = (self.last_ack_t or self.now()) - (self.first_send_t or 0.0)
        duration = max(duration, 1e-9)

        summary = {
            "run_name": self.run_name,
            "meta": self.meta,
            "events": dict(sorted(self.counts.items())),
            "bytes_acked": self.bytes_acked,
            "transfer_duration_s": round(duration, 6),
            "throughput_Bps": round(self.bytes_acked / duration, 2),
            "throughput_Mbps": round(self.bytes_acked * 8 / duration / 1e6, 4),
            "data_packets_sent": sends,
            "retransmissions": retx,
            "total_packets_sent": total_pkts_sent,
            "retransmission_overhead_pct": (
                round(100.0 * retx / total_pkts_sent, 3) if total_pkts_sent else 0.0
            ),
            "timeouts": self.counts.get("TIMEOUT", 0),
            "naks_received": self.counts.get("NAK", 0),
        }
        if extra:
            summary.update(extra)

        with open(self.path_summary, "w") as f:
            json.dump(summary, f, indent=2)
        return summary

    def close(self):
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
