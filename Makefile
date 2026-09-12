# macOS/Linux task runner. Windows: use `tasks.ps1` instead (same targets).

PY      ?= python3
VPY     ?= ./.venv/bin/python
FILE    ?= test_files/medium.bin
LOSS    ?= 0.08
DELAY   ?= 0.02
JITTER  ?= 0.005

.PHONY: help venv testfiles test smoke experiment burst plots demo clean

help:
	@echo "make venv        - create .venv and install matplotlib"
	@echo "make testfiles   - generate test_files/*.bin"
	@echo "make test        - unit-test common/ modules"
	@echo "make smoke       - one baseline + one improved transfer with integrity check"
	@echo "make experiment  - loss-rate sweep (both protocols) -> results/"
	@echo "make burst       - recovery-after-loss-burst experiment -> results/"
	@echo "make plots       - regenerate all graphs from results/"
	@echo "make demo        - live side-by-side comparison (FILE/LOSS/DELAY/JITTER vars)"

venv:
	$(PY) -m venv .venv
	./.venv/bin/pip install -q --upgrade pip matplotlib

testfiles:
	$(PY) test_files/make_test_files.py

test:
	$(PY) -m experiments.test_common

smoke:
	$(PY) -m experiments.run_transfer --proto baseline --file $(FILE) --loss $(LOSS) --delay $(DELAY) --jitter $(JITTER)
	$(PY) -m experiments.run_transfer --proto improved --file $(FILE) --loss $(LOSS) --delay $(DELAY) --jitter $(JITTER)

experiment:
	$(VPY) -m experiments.run_experiment --file $(FILE) --loss 0 0.01 0.05 0.10 --repeats 3 --delay $(DELAY) --jitter $(JITTER)

burst:
	$(VPY) -m experiments.run_experiment --file test_files/large.bin --loss 0.05 --repeats 5 \
		--delay $(DELAY) --jitter 0.006 --burst-at 1.5 --burst-len 0.8 --base-port 5600

plots:
	$(VPY) -m experiments.plot_results --cwnd-loss 0.05

demo:
	$(VPY) -m experiments.demo --file $(FILE) --loss $(LOSS) --delay $(DELAY) --jitter $(JITTER)

clean:
	rm -f results/*.csv results/*.json results/*.png results/*.received.bin
