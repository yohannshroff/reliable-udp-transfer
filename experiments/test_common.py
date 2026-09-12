"""Quick unit tests for the shared common/ modules (no network needed).

Run:  python -m experiments.test_common
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import packet as pkt
from common.channel_sim import Channel, make_udp_socket
from common.congestion import DAIMD, LossEpochGate, CWND_MIN


def test_packet_roundtrip():
    payload = b"hello world" * 10
    raw = pkt.encode(42, 7, pkt.DATA | pkt.ACK, payload)
    p = pkt.decode(raw)
    assert p is not None
    assert p.seq == 42 and p.ack == 7
    assert p.flags == (pkt.DATA | pkt.ACK)
    assert p.checksum_valid is True
    assert p.payload == payload
    print("ok  packet roundtrip")


def test_packet_empty_payload():
    raw = pkt.encode(0, 0, pkt.FIN)
    p = pkt.decode(raw)
    assert p.checksum_valid and p.payload == b"" and p.flags == pkt.FIN
    print("ok  packet empty payload")


def test_packet_corruption_detected():
    raw = bytearray(pkt.encode(100, 0, pkt.DATA, b"abcdefgh"))
    raw[pkt.HEADER_SIZE + 3] ^= 0xFF  # flip a payload bit
    p = pkt.decode(bytes(raw))
    assert p is not None and p.checksum_valid is False
    print("ok  packet corruption detected")


def test_packet_truncated_header():
    assert pkt.decode(b"\x00\x01\x02") is None
    print("ok  packet truncated header -> None")


def test_channel_empirical_loss_rate():
    s = make_udp_socket(("127.0.0.1", 0))
    dst = make_udp_socket(("127.0.0.1", 0))
    dst_addr = dst.getsockname()
    ch = Channel(s, loss_rate=0.30, seed=1, name="t")
    n = 5000
    for i in range(n):
        ch.sendto(b"x", dst_addr)
    emp = ch.empirical_loss_rate()
    assert 0.27 <= emp <= 0.33, emp
    print(f"ok  channel loss rate ~30%% (empirical {emp:.3f})")
    ch.close()
    dst.close()


def test_channel_deterministic_with_seed():
    def drops_for_seed(seed):
        s = make_udp_socket(("127.0.0.1", 0))
        d = make_udp_socket(("127.0.0.1", 0))
        ch = Channel(s, loss_rate=0.2, seed=seed, name="t")
        for i in range(500):
            ch.sendto(b"y", d.getsockname())
        out = ch.dropped
        ch.close()
        d.close()
        return out
    assert drops_for_seed(7) == drops_for_seed(7)
    assert drops_for_seed(7) != drops_for_seed(8)
    print("ok  channel deterministic per seed")


def test_daimd_increase_decreasing():
    cc = DAIMD(init_cwnd=4.0)
    start = cc.cwnd
    cc.on_ack()
    first = cc.cwnd - start
    for _ in range(200):
        cc.on_ack()
    before = cc.cwnd
    cc.on_ack()
    later = cc.cwnd - before
    assert later < first, (first, later)
    print(f"ok  DAIMD increase shrinks as cwnd grows ({first:.4f} -> {later:.5f})")


def test_daimd_multiplicative_decrease():
    cc = DAIMD(init_cwnd=9.0)
    cc.on_loss()
    assert abs(cc.cwnd - 8.0) < 1e-9, cc.cwnd
    for _ in range(100):
        cc.on_loss()
    assert cc.cwnd >= CWND_MIN
    print("ok  DAIMD multiplicative decrease + floor")


def test_loss_epoch_gate_collapses_burst():
    g = LossEpochGate()
    # first loss in a window that has sent up to seq 20, base at 5
    assert g.should_cut(base=5, highest_sent=20) is True
    # more losses in the same epoch (base hasn't passed 20) -> no extra cut
    assert g.should_cut(base=6, highest_sent=25) is False
    assert g.should_cut(base=20, highest_sent=30) is False
    # base moves past the recovery point -> next loss is a new epoch
    assert g.should_cut(base=21, highest_sent=30) is True
    assert g.epochs == 2
    print("ok  LossEpochGate collapses a burst into one cut")


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nall {len(fns)} common/ tests passed")


if __name__ == "__main__":
    main()
