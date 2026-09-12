# Custom Reliable File-Transfer Protocol over UDP

Course project (BCSE308L/P — Computer Networks & Lab, VIT). A reliable
file-transfer protocol built on top of UDP, in two versions that are run under
**identical simulated loss conditions** and compared:

| | ARQ | Retransmission timer | Congestion control |
|---|---|---|---|
| **baseline** (`baseline/`) | Go-Back-N | **fixed** RTO (200 ms) | DAIMD (shared) |
| **improved** (`improved/`) | **Selective Repeat** + SACK fast-retransmit | **adaptive** RTO (Jacobson/Karels SRTT/RTTVAR, Karn) | DAIMD (shared) |

Everything in `common/` — packet framing, the simulated lossy channel, the
DAIMD controller, and the logger — is **identical for both versions**, so any
measured difference is attributable only to the ARQ + RTO change. Reference:
Gu & Grossman, "UDT: UDP-based Data Transfer for High-Speed Wide Area
Networks," *Computer Networks* 51(7), 2007.

## Layout

```
common/
  packet.py        fixed 13-byte header + checksum  (!IIBHH: seq, ack, flags, checksum, payload_len)
  channel_sim.py   wraps sendto(): random loss, one-way delay+jitter (FIFO), reordering, timed loss burst
  congestion.py    DAIMD window controller + LossEpochGate (one CC cut per congestion epoch)
  logger.py        per-run event CSV  +  <run>.summary.json
baseline/          sender.py (GBN, fixed RTO), receiver.py (cumulative ACK, NAK on gap)
improved/          sender.py (SR, adaptive RTO, SACK fast-retx), receiver.py (out-of-order buffer, per-packet ACK), rttest.py
experiments/
  test_common.py       unit tests for common/  (no network)
  run_transfer.py       one transfer as subprocesses + SHA-256 integrity check
  run_experiment.py     sweep loss rates x repeats x both protocols -> results/experiment_index.csv
  plot_results.py       the Review-2 / Review-3 graphs -> results/*.png
  demo.py               live side-by-side comparison for the one-to-one demo
test_files/          make_test_files.py generates tiny/small/medium/large.bin
results/             per-run CSVs, aggregate CSVs, generated PNGs
Makefile             macOS/Linux task runner
tasks.ps1            Windows/PowerShell equivalent of the Makefile
```

Runs unmodified on **macOS, Linux, and Windows** — the protocol itself is
pure standard library (`socket`, `struct`, `threading`), no platform-specific
code anywhere.

## Setup

Standard library only for the protocol itself. Plotting needs `matplotlib`,
installed in a local venv so the system Python is untouched.

**macOS / Linux:**

```bash
python3 -m venv .venv
./.venv/bin/pip install matplotlib
python3 test_files/make_test_files.py
```

**Windows (PowerShell):**

```powershell
py -m venv .venv
.\.venv\Scripts\pip install matplotlib
py test_files\make_test_files.py
```

### Windows quick start: `tasks.ps1`

`tasks.ps1` is a drop-in PowerShell equivalent of the `Makefile` below — same
targets, same defaults, auto-detects `py`/`python`/`python3` on PATH:

```powershell
.\tasks.ps1 venv         # create .venv + install matplotlib
.\tasks.ps1 testfiles    # generate test_files\*.bin
.\tasks.ps1 test         # unit tests
.\tasks.ps1 smoke        # one baseline + one improved transfer, integrity-checked
.\tasks.ps1 experiment   # loss-rate sweep -> results\
.\tasks.ps1 burst        # loss-burst experiment -> results\
.\tasks.ps1 plots        # regenerate all graphs
.\tasks.ps1 demo -Loss 0.1
.\tasks.ps1 clean
```

If PowerShell refuses to run it ("running scripts is disabled on this
system"), that's the default execution policy blocking unsigned local
scripts — either run once with:

```powershell
powershell -ExecutionPolicy Bypass -File .\tasks.ps1 test
```

or allow local scripts for your user permanently:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Run it

Unit-test the shared modules:

```bash
python3 -m experiments.test_common
```

One transfer with integrity check (loopback, 5 % loss on each direction,
20 ms ± 5 ms one-way delay):

```bash
python3 -m experiments.run_transfer --proto baseline --file test_files/medium.bin --loss 0.05 --delay 0.02 --jitter 0.005
python3 -m experiments.run_transfer --proto improved --file test_files/medium.bin --loss 0.05 --delay 0.02 --jitter 0.005
```

Manual two-terminal run:

```bash
# terminal 1
python3 -m improved.receiver --port 5001 --out /tmp/got.bin --loss 0.05 --seed 1 --delay 0.02 --jitter 0.005
# terminal 2
python3 -m improved.sender --host 127.0.0.1 --port 5001 --file test_files/medium.bin --loss 0.05 --seed 1 --delay 0.02 --jitter 0.005
```

Live demo (baseline then improved, same file/loss, prints a comparison table):

```bash
./.venv/bin/python -m experiments.demo --file test_files/medium.bin --loss 0.08 --delay 0.02 --jitter 0.005
```

Full experiment sweep + graphs:

```bash
./.venv/bin/python -m experiments.run_experiment --file test_files/medium.bin --loss 0 0.01 0.05 0.10 --repeats 3 --delay 0.02 --jitter 0.005
# recovery-after-burst experiment (writes results/experiment_burst_index.csv)
./.venv/bin/python -m experiments.run_experiment --file test_files/large.bin --loss 0.05 --repeats 5 --delay 0.02 --jitter 0.006 --burst-at 1.5 --burst-len 0.8 --base-port 5600
./.venv/bin/python -m experiments.plot_results --cwnd-loss 0.05
```

`make test` / `make experiment` / `make plots` / `make demo` wrap these on
macOS/Linux; `.\tasks.ps1 test` / `experiment` / `plots` / `demo` are the
Windows equivalents (see above). The `manual two-terminal run` above works
identically on Windows -- just use `py` instead of `python3` and drop the
`./.venv/bin/` prefix in favour of `.\.venv\Scripts\`.

## Results so far (medium.bin, 256 KB, 20 ms ± 5 ms one-way delay, 3 repeats)

| loss | throughput (Mbps) base → impr | retx overhead base → impr | transfer time base → impr |
|---|---|---|---|
| 0 %  | 1.99 → 2.00 | 0 % → 0 %      | 1.05 s → 1.05 s |
| 1 %  | 1.60 → 1.76 | 8.9 % → 3.8 %  | 1.34 s → 1.19 s |
| 5 %  | 1.10 → 1.42 | 30.0 % → 13.7 % | 1.97 s → 1.48 s |
| 10 % | 0.79 → 1.10 | 42.8 % → 23.1 % | 2.68 s → 1.91 s |

Improved: **~2× lower retransmission overhead**, **+25–40 % throughput** under
loss, and much lower run-to-run variance — with no penalty at 0 % loss. It
takes more (cheap, adaptive-RTO) timeouts as a backstop where the baseline
avoids timeouts only by NAK-flooding the whole window.

## Notes on fairness

* Both runs use the same channel PRNG seed for a given (loss, repeat), so they
  see the same drop pattern.
* `channel_sim.py` delivers per destination in FIFO order under jitter;
  genuine reordering happens only via `--reorder`.
* DAIMD and its `LossEpochGate` live in `common/` and are called identically by
  both senders (both pass the window base as the epoch key), so the congestion
  response is the same and the comparison isolates ARQ + RTO.
* Integrity is checked with SHA-256 on every `run_transfer` / `run_experiment`
  run; edge cases covered: < 1-packet file, multi-MB file, 0 % loss.

## Windows notes

* First run may trigger a **Windows Defender Firewall** prompt asking whether
  Python may communicate on private/public networks -- allow it (everything
  here talks over `127.0.0.1`, no inbound connection from outside the machine
  is needed).
* Everything runs identically to macOS/Linux otherwise: same commands, same
  module layout, same output files -- only the venv path (`.venv\Scripts\`
  vs `.venv/bin/`) and the launcher (`py` vs `python3`) differ, which
  `tasks.ps1` already handles for you.
