# C64 LLM Interface - top-level targets

PYTHON ?= python3

.PHONY: all client client-direct test-emu test-emu-hayes run-live clean

all: client

client:
	$(MAKE) -C c64_client

client-direct:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct

client-hayes-local:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=127.0.0.1

# Automated end-to-end test in VICE: mock LLM -> proxy -> emulated C64.
# Direct mode: ACIA pipe wired straight to the proxy (no modem emulation).
test-emu: client-direct
	$(PYTHON) emu/test_e2e.py --mode direct

# Hayes mode: tcpser emulates the modem, exercising the AT dial flow
# that real C64 Ultimate hardware uses.
test-emu-hayes: client-hayes-local
	$(PYTHON) emu/test_e2e.py --mode hayes

# Interactive session against the real API configured in c64llm_proxy/config.toml
run-live: client-direct
	$(PYTHON) emu/test_e2e.py --mode direct --live --timeout 300

clean:
	$(MAKE) -C c64_client clean
	rm -rf emu/artifacts
