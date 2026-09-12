"""DAIMD congestion control -- SHARED, identical for both protocol versions.

This lives in ``common/`` on purpose: the project's central claim is that the
*only* difference between baseline and improved is the ARQ scheme (Go-Back-N vs
Selective Repeat) and the retransmission timer (fixed vs adaptive RTO). The
congestion controller must therefore be byte-for-byte the same for both, so it
is defined once here and imported by both senders.

UDT uses a rate-based DAIMD (Decreasing AIMD) where the additive increase gets
smaller as the sending rate approaches the estimated link capacity, and a
multiplicative decrease of 1/9 on a congestion event. We do not estimate link
bandwidth on a loopback channel, so we use the standard *window-based* analogue:

    increase (per ACK)     :  cwnd += 1 / cwnd      -> increment shrinks as cwnd grows
    decrease (per loss ev.):  cwnd *= 8/9           -> UDT's 1/9 multiplicative cut
    cwnd is clamped to [CWND_MIN, CWND_MAX]

A congestion *event* is not a single lost packet: every loss detected before the
window has fully turned over (i.e. before ``base`` passes the seq that was in
flight when the first loss of the epoch was seen) collapses into ONE cut. That
rule is implemented by :class:`LossEpochGate` and is used identically by both
senders -- without it, Selective Repeat (whose ``base`` creeps forward one
packet at a time) would take dozens of cuts where Go-Back-N takes one, and the
comparison would be measuring CC unfairness instead of ARQ/RTO.
"""

CWND_MIN = 1.0
CWND_MAX = 10000.0
DECREASE_FACTOR = 8.0 / 9.0  # UDT cuts the rate by 1/9 on a congestion event


class DAIMD:
    def __init__(self, init_cwnd=4.0):
        self.cwnd = float(init_cwnd)
        self.n_increase = 0
        self.n_decrease = 0

    def on_ack(self, n=1):
        """Additive, decreasing increase -- call once per newly-acked packet."""
        for _ in range(n):
            self.cwnd += 1.0 / self.cwnd
        if self.cwnd > CWND_MAX:
            self.cwnd = CWND_MAX
        self.n_increase += 1

    def on_loss(self):
        """Unconditional multiplicative decrease. Callers decide *when* to call
        this via :class:`LossEpochGate`."""
        self.cwnd = max(CWND_MIN, self.cwnd * DECREASE_FACTOR)
        self.n_decrease += 1
        return True

    @property
    def window(self):
        """Integer window (packets in flight allowed), never below 1."""
        return max(1, int(self.cwnd))


class LossEpochGate:
    """Collapses a burst of losses into a single congestion event.

    Usage (identical in both senders):

        gate = LossEpochGate()
        ...
        # on a detected loss of packet `seq`, with `highest_sent` = the largest
        # seq ever transmitted and `base` = lowest unacked seq:
        if gate.should_cut(base, highest_sent):
            cc.on_loss()

    The first loss of an epoch opens a "recovery" region ending at
    ``highest_sent``. Further losses are ignored for CC purposes until ``base``
    advances past that recovery point, at which point the next loss starts a
    fresh epoch.
    """

    def __init__(self):
        self.in_recovery = False
        self.recover_point = -1
        self.epochs = 0

    def should_cut(self, base, highest_sent):
        if self.in_recovery:
            if base > self.recover_point:
                self.in_recovery = False
            else:
                return False
        self.in_recovery = True
        self.recover_point = highest_sent
        self.epochs += 1
        return True
