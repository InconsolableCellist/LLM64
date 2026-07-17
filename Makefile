# C64 LLM Interface - top-level targets

PYTHON ?= python3

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
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=127.0.0.1 DEBUG_CLIENT=1

# Automated end-to-end test in VICE: mock LLM -> proxy -> emulated C64.
# Direct mode: ACIA pipe wired straight to the proxy (no modem emulation).
test-emu: client-direct
	$(PYTHON) emu/test_e2e.py --mode direct

# Hayes mode: tcpser emulates the modem, exercising the AT dial flow
# that real C64 Ultimate hardware uses.
test-emu-hayes: client-hayes-local
	$(PYTHON) emu/test_e2e.py --mode hayes

# Sustained streaming: ~3KB response at full speed, asserts zero CRC failures
test-emu-long:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct DEBUG_CLIENT=1 TEST_MESSAGE=LONGTEST
	$(PYTHON) emu/test_e2e.py --mode direct --expect "streaming test" --timeout 180

# Interactive TUI driven end-to-end via emulated keyboard input
test-emu-tui: client-tui-direct
	$(PYTHON) emu/test_e2e.py --mode direct --tui

test-all: test-emu test-emu-long test-emu-hayes test-emu-tui

# Interactive session against the real API configured in c64llm_proxy/config.toml
run-live: client-direct
	$(PYTHON) emu/test_e2e.py --mode direct --live --timeout 300

clean:
	$(MAKE) -C c64_client clean
	rm -rf emu/artifacts
