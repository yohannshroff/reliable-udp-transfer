"""Adaptive retransmission-timeout estimator (improved protocol only).

Jacobson/Karels TCP-style estimator (RFC 6298):

    first sample:   SRTT   = R
                    RTTVAR = R / 2
    later samples:  RTTVAR = (1-beta)*RTTVAR + beta*|SRTT - R|
                    SRTT   = (1-alpha)*SRTT + alpha*R
    always:         RTO    = SRTT + max(G, K*RTTVAR),  clamped to [min, max]

with alpha = 1/8, beta = 1/4, K = 4. Karn's algorithm: RTT samples are taken
only from packets that were never retransmitted; on a timeout the RTO is
doubled (exponential backoff) and left there until a fresh clean sample.
"""


class RttEstimator:
    ALPHA = 1.0 / 8.0
    BETA = 1.0 / 4.0
    K = 4.0
    G = 0.010  # clock granularity, seconds

    def __init__(self, initial_rto=1.0, min_rto=0.05, max_rto=2.0, max_backoffs=4):
        self.srtt = None
        self.rttvar = None
        self.min_rto = min_rto
        self.max_rto = max_rto
        self.max_backoffs = max_backoffs
        self._rto = initial_rto
        self._backoffs = 0
        self.n_samples = 0

    @property
    def rto(self):
        return self._rto

    @property
    def srtt_ms(self):
        return self.srtt * 1000.0 if self.srtt is not None else -1.0

    def add_sample(self, r):
        """Feed one clean RTT sample (seconds). Ignores non-positive values."""
        if r <= 0:
            return
        self.n_samples += 1
        if self.srtt is None:
            self.srtt = r
            self.rttvar = r / 2.0
        else:
            self.rttvar = (1 - self.BETA) * self.rttvar + self.BETA * abs(self.srtt - r)
            self.srtt = (1 - self.ALPHA) * self.srtt + self.ALPHA * r
        self._rto = self._clamp(self.srtt + max(self.G, self.K * self.rttvar))
        self._backoffs = 0  # a clean sample clears Karn backoff (RFC 6298)

    def backoff(self):
        """Exponential backoff on retransmission (Karn), capped so a total
        blackout can't inflate the RTO without bound."""
        if self._backoffs >= self.max_backoffs:
            return
        self._backoffs += 1
        self._rto = self._clamp(self._rto * 2.0)

    def _clamp(self, v):
        return max(self.min_rto, min(self.max_rto, v))
