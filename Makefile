# C64 LLM Interface - top-level targets

PYTHON ?= python3
# Test proxy port: distinct from 6400 so a live proxy can keep running
TESTPORT ?= 6464

.PHONY: all client client-direct test-emu test-emu-hayes run-live clean

all: client

client:
	$(MAKE) -C c64_client

# TUI builds
client-tui-direct:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct

# Scripted debug-session builds (drive the automated protocol tests)
client-direct:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct DEBUG_CLIENT=1

client-hayes-local:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=127.0.0.1 SERVER_PORT=$(TESTPORT) DEBUG_CLIENT=1

# Automated end-to-end test in VICE: mock LLM -> proxy -> emulated C64.
# Direct mode: ACIA pipe wired straight to the proxy (no modem emulation).
test-emu: client-direct
	$(PYTHON) emu/test_e2e.py --mode direct --proxy-port $(TESTPORT)

# Hayes mode: tcpser emulates the modem, exercising the AT dial flow
# that real C64 Ultimate hardware uses.
test-emu-hayes: client-hayes-local
	$(PYTHON) emu/test_e2e.py --mode hayes --proxy-port $(TESTPORT)

# Sustained streaming: ~3KB response at full speed, asserts zero CRC failures
test-emu-long:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct DEBUG_CLIENT=1 TEST_MESSAGE=LONGTEST
	$(PYTHON) emu/test_e2e.py --mode direct --expect "streaming test" --timeout 180 --proxy-port $(TESTPORT)

# Same, at real C64 speed with every-chunk-arrived assertion: catches
# IRQ saturation / dropped frames that warp mode hides
test-emu-long-rt:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct DEBUG_CLIENT=1 TEST_MESSAGE=LONGTEST
	$(PYTHON) emu/test_e2e.py --mode direct --no-warp --expect "streaming test" --assert-all-chunks --timeout 200 --proxy-port $(TESTPORT)

# Interactive TUI driven end-to-end via emulated keyboard input
test-emu-tui: client-tui-direct
	$(PYTHON) emu/test_e2e.py --mode direct --tui --proxy-port $(TESTPORT)

# Real X11 keystrokes through the emulated keyboard matrix (exercises
# the custom scanner + n-key rollover). Needs a desktop session and
# steals focus, so not part of test-all.
test-emu-matrix: client-tui-direct
	$(PYTHON) emu/test_matrix.py

# Response watchdog: a request that gets no reply must time out, not hang.
# Short watchdog build so the test is quick.
test-emu-watchdog:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct MODE80=1 CFLAGS_EXTRA=-DWATCHDOG_UNITS=1
	$(PYTHON) emu/test_watchdog.py

test-all: test-emu test-emu-long test-emu-long-rt test-emu-hayes test-emu-tui test-emu-tui-80 test-emu-watchdog

# Interactive TUI session against the real API from c64llm_proxy/config.toml
run-live: client-tui-direct
	./emu/run_live.sh

# Automated smoke test against the real API (model may need time to load)
test-live: client-tui-direct
	$(PYTHON) emu/test_e2e.py --mode direct --tui --live --timeout 300

clean:
	$(MAKE) -C c64_client clean
	rm -rf emu/artifacts

# Build for real hardware and run it on the C64 Ultimate over the network.
# The proxy lives on mlboy (192.168.1.21), colocated with llama.cpp.
C64U_IP ?= 192.168.1.64
C64_PROXY_IP ?= 192.168.1.21
deploy-c64u:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP)
	$(PYTHON) emu/u64_telnet.py c64_client/build/c64llm.prg

# Soft-80-column variants
client-tui-direct-80:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct MODE80=1

test-emu-tui-80: client-tui-direct-80
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --proxy-port $(TESTPORT)

# Same run against a DIAG=1 client, then read the crash post-mortem
# block back out: proves the instrumentation records what the user will
# PEEK after a drop to BASIC, and reports the C-stack high-water mark
# under a realistic workload (streamed SID + overlay module loads).
test-emu-diag:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct MODE80=1 DIAG=1
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --diag \
		--proxy-port $(TESTPORT)

deploy-c64u-80:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) MODE80=1
	$(PYTHON) emu/u64_telnet.py c64_client/build/c64llm.prg

# The canonical deploy: one d64 with the client + overlay module,
# mounted on the Ultimate's 1541 (JiffyDOS fastload applies, config
# saves persist inside the image, burnable to a real floppy).
deploy-c64u-disk-80:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) MODE80=1
	$(MAKE) -C c64_client disk
	$(PYTHON) emu/u64_telnet.py c64_client/build/c64llm.d64
