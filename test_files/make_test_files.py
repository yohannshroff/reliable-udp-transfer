"""Generate deterministic sample files of varying sizes for transfer tests."""
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

SIZES = {
    "tiny.bin": 200,             # < 1 packet
    "small.bin": 8 * 1024,       # a few packets
    "medium.bin": 256 * 1024,    # many windows
    "large.bin": 2 * 1024 * 1024,
}


def main():
    rng = random.Random(12345)
    for name, size in SIZES.items():
        path = os.path.join(HERE, name)
        data = bytes(rng.getrandbits(8) for _ in range(size))
        with open(path, "wb") as f:
            f.write(data)
        print(f"wrote {path} ({size} B)")


if __name__ == "__main__":
    main()
