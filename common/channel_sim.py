"""Artificial lossy/delaying channel that wraps a UDP socket.

Both the baseline and the improved protocol send through an instance of
``Channel`` so that the two runs experience *statistically identical* channel
behaviour (same loss rate, same PRNG seed, same delay distribution). Only the
sender/receiver logic differs between versions -- this file must stay identical
for both.

Impairments applied on every ``sendto``:

* random packet loss at a configurable rate
* optional per-packet one-way delay (``delay_mean`` +/- uniform ``delay_jitter``)
* optional reordering: with probability ``reorder_rate`` a datagram is given a
  large extra delay so later datagrams overtake it. Plain jitter does NOT
  reorder -- a single background delivery thread with a priority queue keeps
  per-destination FIFO order unless a packet was explicitly picked for
  reordering.
* optional timed total-loss burst (``burst_at`` / ``burst_len``) for the
  "recovery time after a loss burst" experiment.

Counters let the empirical drop rate be sanity-checked against the configured
rate (see the testing checklist).
"""

import heapq
import itertools
import random
import socket
import threading
import time


class Channel:
    def __init__(
        self,
        sock,
        loss_rate=0.0,
        delay_mean=0.0,
        delay_jitter=0.0,
        reorder_rate=0.0,
        seed=None,
        burst_at=None,
        burst_len=0.0,
        name="chan",
    ):
        self.sock = sock
        self.loss_rate = loss_rate
        self.delay_mean = delay_mean
        self.delay_jitter = delay_jitter
        self.reorder_rate = reorder_rate
        self.burst_at = burst_at
        self.burst_len = burst_len
        self.name = name

        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()
        self._t0 = time.monotonic()
        self._last_delivery_at = {}   # addr -> latest scheduled delivery time

        # counters
        self.sent = 0            # datagrams actually put on the wire
        self.dropped = 0         # dropped by random loss
        self.dropped_burst = 0   # dropped by the timed burst
        self.delayed = 0         # deliberately reordered

        # single delivery thread + min-heap keyed by (deliver_at, seq)
        self._heap = []
        self._counter = itertools.count()
        self._cv = threading.Condition()
        self._closed = False
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- helpers ------------------------------------------------------
    def _rand(self):
        with self._rng_lock:
            return self._rng.random()

    def _uniform(self, a, b):
        with self._rng_lock:
            return self._rng.uniform(a, b)

    def _in_burst(self):
        if self.burst_at is None or self.burst_len <= 0:
            return False
        elapsed = time.monotonic() - self._t0
        return self.burst_at <= elapsed < (self.burst_at + self.burst_len)

    def _raw_send(self, data, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _run(self):
        while True:
            with self._cv:
                while not self._closed and not self._heap:
                    self._cv.wait()
                if self._closed and not self._heap:
                    return
                deliver_at, _, data, addr = self._heap[0]
                now = time.monotonic()
                if deliver_at > now:
                    self._cv.wait(timeout=deliver_at - now)
                    continue
                heapq.heappop(self._heap)
            self._raw_send(data, addr)

    # -- public API -------------------------------------------------
    def sendto(self, data, addr):
        """Impairment-applying replacement for ``sock.sendto()``."""
        if self._in_burst():
            self.dropped_burst += 1
            return
        if self.loss_rate > 0 and self._rand() < self.loss_rate:
            self.dropped += 1
            return

        now = time.monotonic()
        delay = 0.0
        if self.delay_mean > 0 or self.delay_jitter > 0:
            jitter = self._uniform(-self.delay_jitter, self.delay_jitter) if self.delay_jitter else 0.0
            delay = max(0.0, self.delay_mean + jitter)

        reordered = self.reorder_rate > 0 and self._rand() < self.reorder_rate
        deliver_at = now + delay

        if reordered:
            base = self.delay_mean if self.delay_mean > 0 else 0.01
            deliver_at += self._uniform(0.5 * base, 2.0 * base)
            self.delayed += 1
        elif delay > 0:
            prev = self._last_delivery_at.get(addr, 0.0)
            if deliver_at < prev:
                deliver_at = prev            # preserve FIFO despite jitter
            self._last_delivery_at[addr] = deliver_at

        self.sent += 1
        if self.delay_mean <= 0 and self.delay_jitter <= 0 and not reordered:
            # no timing model in play -> send straight through, order preserved
            # by the caller; nothing is ever sitting in the heap
            self._raw_send(bytes(data), addr)
            return
        with self._cv:
            heapq.heappush(
                self._heap, (deliver_at, next(self._counter), bytes(data), addr)
            )
            self._cv.notify()

    def recvfrom(self, bufsize):
        """Pass-through; impairments are applied on the send side only."""
        return self.sock.recvfrom(bufsize)

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._worker.join(timeout=1.0)
        self.sock.close()

    # -- diagnostics ----------------------------------------------
    def empirical_loss_rate(self):
        attempted = self.sent + self.dropped
        return (self.dropped / attempted) if attempted else 0.0

    def stats(self):
        return {
            "name": self.name,
            "configured_loss_rate": self.loss_rate,
            "attempted": self.sent + self.dropped + self.dropped_burst,
            "sent": self.sent,
            "dropped_random": self.dropped,
            "dropped_burst": self.dropped_burst,
            "reordered": self.delayed,
            "empirical_loss_rate": round(self.empirical_loss_rate(), 4),
        }


def make_udp_socket(bind_addr=None):
    """Create a UDP socket, optionally bound to (host, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if bind_addr is not None:
        s.bind(bind_addr)
    return s
