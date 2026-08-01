# LLM64 - top-level targets
#
# To build everything shippable, one command:
#
#     make release
#
# It produces the C64 disk, both Windows clients and their floppy
# image, the Linux proxy binary and - if the Wine Python is set up -
# the Windows proxy exe, then prints what it made. See the `release`
# rule below for the artifact list and how to point them at a proxy.

PYTHON ?= python3
# VICE tools (x64sc, c1541): native install if present, net.sf.VICE flatpak
# otherwise. See emu/vice-run.sh.
VICE_RUN ?= ./emu/vice-run.sh
# Test proxy port: distinct from 6400 so a live proxy can keep running
TESTPORT ?= 6464

.PHONY: all client client-direct test-emu test-emu-hayes run-live clean \
        win win-floppy proxy-bin proxy-bin-win release disk disk-vice \
        manifest dirty-check

# The bare client PRG, 40-column and unbundled: the quick compile check,
# not a shippable. `make release` is the one that builds artifacts.
all: client

client:
	$(MAKE) -C c64_client

# ---------------------------------------------------------------------
# release: every artifact this machine can produce, in one command.
#
#   c64_client/build/llm64.d64          C64 boot disk (client + overlays)
#   c64_client/build/llm64-vice.d64     the same disk for VICE, no tcpser
#   win311_client/build/LLM64.EXE       Windows 3.x client, 16-bit NE
#   win311_client/build/LLM32.EXE       Windows 10/11 client, 32-bit PE
#   win311_client/build/llm64.img       1.44 MB floppy: LLM64.EXE + INI
#   llm64_proxy/dist/llm64-proxy        Linux proxy, self-contained
#   llm64_proxy/dist/llm64-proxy.exe    Windows proxy, self-contained
#
# The proxy address baked into the C64 disk and the floppy's INI comes
# from C64_PROXY_IP/C64_PROXY_PORT (below) and WIN_PROXY_IP/PORT; both
# are only defaults, since the C64 disk ships cfg-free and reads its
# NetConfig from the editor on first boot, and the Windows client reads
# the INI beside itself.
#
# The Windows proxy exe is the one soft failure here: it needs the Wine
# Python from llm64_proxy/tools/win-build-setup.sh, and without it the
# rest of the release still completes. Nothing else is allowed to fail
# quietly.
release: dirty-check disk-vice disk win win-floppy proxy-bin
	@$(MAKE) --no-print-directory proxy-bin-win || \
	    echo "*** llm64-proxy.exe SKIPPED - everything else was built"
	@$(MAKE) --no-print-directory manifest

# The C64 title bar carries the git hash, with a '+' for a dirty tree,
# so a release built from uncommitted work is stamped as one. A warning
# rather than a refusal: test builds are the normal case here.
dirty-check:
	@if [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
	    echo; echo "*** working tree is dirty - the build hash will be stamped '+'"; \
	    echo "*** commit first if this is a real release"; echo; fi

RELEASE_ARTIFACTS = c64_client/build/llm64.d64 \
                    c64_client/build/llm64-vice.d64 \
                    win311_client/build/LLM64.EXE \
                    win311_client/build/LLM32.EXE \
                    win311_client/build/llm64.img \
                    llm64_proxy/dist/llm64-proxy \
                    llm64_proxy/dist/llm64-proxy.exe

# What actually landed, with its age: a stale artifact from an earlier
# run looks exactly like a fresh one in an `ls`, so the timestamp is
# the point of this.
manifest:
	@echo; echo "Release artifacts:"
	@for f in $(RELEASE_ARTIFACTS); do \
	    if [ -f "$$f" ]; then \
	        printf '  %-36s %7s  %s\n' "$$f" \
	            "$$(du -h "$$f" | cut -f1)" \
	            "$$(date -r "$$f" '+%Y-%m-%d %H:%M')"; \
	    else printf '  %-36s %7s\n' "$$f" "-- missing"; fi; \
	done; echo

# The C64 boot disk. MODE80=1 is not optional: the overlay modules
# (llm64.prg.1 .. .5) exist only because c64-soft80.cfg declares them,
# and that config is only linked in under MODE80 - a plain build emits
# no modules and the disk rule refuses to run. The clean is there
# because the objects carry no dependency on the flags that made them.
#
# clean-obj, not clean: `release` builds disk-vice first and this one
# second, and a full clean here would delete the VICE image that just
# landed. Building in that order also leaves build/llm64.prg holding the
# hayes client that matches llm64.d64, which is what emu/run_emu.sh
# autostarts beside the mounted disk - modules are linked against the
# PRG they were built with, so a mismatched pair crashes on F1.
disk:
	$(MAKE) -C c64_client clean-obj
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT) MODE80=1
	$(MAKE) -C c64_client disk

# The VICE disk: same client, same overlays, built CONNECT=direct so the
# ACIA *is* the socket. VICE dials the proxy through -rsdev1 and the
# client never speaks Hayes, which is what saves a VICE user from having
# to install and run tcpser. CONNECT_DIRECT also compiles out the
# first-boot config editor, so this disk asks for nothing: the proxy
# address lives in the x64sc command line instead. See
# c64_client/README.md#in-vice.
disk-vice:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct MODE80=1
	$(MAKE) -C c64_client disk-vice

# Both Windows client binaries: LLM64.EXE (16-bit) and LLM32.EXE.
win:
	$(MAKE) -C win311_client both

# The 1.44 MB floppy, for a VM or a real diskette. Its INI points at the
# same proxy the C64 disk dials; override for a QEMU guest, where the
# host is 10.0.2.2 through slirp:
#   make win-floppy WIN_PROXY_IP=10.0.2.2 WIN_PROXY_PORT=6410
WIN_PROXY_IP   ?= $(C64_PROXY_IP)
WIN_PROXY_PORT ?= $(C64_PROXY_PORT)
win-floppy: win
	$(MAKE) -C win311_client floppy VMHOST=$(WIN_PROXY_IP) VMPORT=$(WIN_PROXY_PORT)

# The packaged Linux proxy (PyInstaller): dist/llm64-proxy. Needs the
# one-time .venv-build from llm64_proxy/PACKAGING.md.
proxy-bin:
	cd llm64_proxy && .venv-build/bin/pyinstaller llm64.spec --noconfirm

# The packaged Windows proxy: dist/llm64-proxy.exe. PyInstaller does
# not cross-compile, so this is a Windows Python doing the work - one
# installed under Wine by llm64_proxy/tools/win-build-setup.sh (run it
# once; it downloads CPython and pip-installs the dependencies).
proxy-bin-win:
	cd llm64_proxy && ./tools/win-build.sh

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

# Interactive TUI session against the real API from llm64_proxy/config.toml
run-live: client-tui-direct
	./emu/run_live.sh

# Automated smoke test against the real API (model may need time to load)
test-live: client-tui-direct
	$(PYTHON) emu/test_e2e.py --mode direct --tui --live --timeout 300

clean:
	$(MAKE) -C c64_client clean
	rm -rf emu/artifacts

# Build for real hardware and run it on the C64 Ultimate over the network.
#
# Addresses: the C64 Ultimate to deploy to, and the proxy the client
# dials (colocate the proxy with your model). ./run.sh reads both from
# run.conf and passes them down, so set them there rather than editing
# here; these defaults are only for calling make directly.
C64U_IP ?= 192.168.1.64
C64_PROXY_IP ?= 192.168.1.21
C64_PROXY_PORT ?= 6400
deploy-c64u:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT)
	$(PYTHON) emu/u64_telnet.py c64_client/build/llm64.prg $(C64U_IP)

# Soft-80-column variants
client-tui-direct-80:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=direct MODE80=1

test-emu-tui-80: client-tui-direct-80
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --proxy-port $(TESTPORT)

# Same run with NOTHING on IEC device 4: /print must be refused at the
# BEGIN handshake and leave the client usable, rather than sitting on a
# channel that never opened (docs/14 §7.3). Not in test-all - it is the
# whole 80-column suite again for one assertion; run it when the print
# path changes.
test-emu-print-fail: client-tui-direct-80
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --no-printer \
		--proxy-port $(TESTPORT)

# And again with [printer] backend = "cups" (docs/14 §13): the proxy
# spools the composed document to a stubbed lp, device 4 stays OFF the
# bus, and /print still works - the no-C64-printer path. Same reason as
# above for staying out of test-all.
test-emu-print-cups: client-tui-direct-80
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --cups \
		--proxy-port $(TESTPORT)

# backend = "both": one composed document, delivered to the stubbed CUPS
# queue AND printed on device 4, one leg after the other. This is the
# configuration a Pi print bridge behind a real C64U runs (docs/14 §13.6).
test-emu-print-both: client-tui-direct-80
	$(PYTHON) emu/test_e2e.py --mode direct --tui --cols80 --cups both \
		--proxy-port $(TESTPORT)

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
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT) MODE80=1
	$(PYTHON) emu/u64_telnet.py c64_client/build/llm64.prg $(C64U_IP)

# The canonical deploy: one d64 with the client + overlay module,
# mounted on the Ultimate's 1541 (JiffyDOS fastload applies, config
# saves persist inside the image, burnable to a real floppy).
#
# Ships cfg-free by default: first boot opens the config editor, which is
# the real first-run experience every new user gets (address + wire
# speed), so the distributed disk and what a buyer sees are identical.
# INJECT_CFG=1 bakes the maintainer's NetConfig instead - a convenience
# for iterating on your own hardware without retyping the address.
deploy-c64u-disk-80: INJECT_CFG ?= 0
deploy-c64u-disk-80:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT) MODE80=1
	$(MAKE) -C c64_client disk
	@if [ "$(INJECT_CFG)" = "1" ]; then $(MAKE) inject-cfg; \
	 else echo "INJECT_CFG=0: cfg-free, first boot opens the config editor"; fi
	$(PYTHON) emu/u64_telnet.py c64_client/build/llm64.d64 $(C64U_IP)

# Write the maintainer's NetConfig into the freshly built disk. NOT for
# distribution disks - those stay cfg-free by design so a new user meets
# the config editor.
#
# Purely a convenience: it saves retyping the address on every deploy.
# (An earlier version of this comment claimed a cfg-free disk came up
# with a dead F1 menu. It does not - see HANDOFF.md, RETRACTED.)
#
# NetConfig blob: 2 dummy bytes for the PRG header cbm_load skips, magic
# C6 01, then host[32] and port[6], NUL-padded. Digits and dots are
# identical in PETSCII and ASCII, so no conversion needed.
CFG_DISK ?= c64_client/build/llm64.d64
inject-cfg:
	$(PYTHON) -c "open('c64_client/build/user.cfg','wb').write(b'\x00\x10\xc6\x01'+b'$(C64_CFG_HOST)'.ljust(32,b'\0')+b'$(C64_CFG_PORT)'.ljust(6,b'\0'))"
	$(VICE_RUN) c1541 $(CFG_DISK) -write c64_client/build/user.cfg llm64.cfg

# The free (shareware) disk: identical contents plus the intro PRG as the
# first file, so Run Disk boots the intro and the intro chain-loads the
# client. Rule 1 applies as always - commit first, then build, or the
# title-bar hash inside the disk is a lie.
#
# It lands in /Flash as llm64-free.d64, beside the registered
# llm64.d64, so both stay bootable from the Ultimate's menu.
#
# NO inject-cfg here, deliberately. A shareware disk ships cfg-free and
# meets its user with the config editor, so this target IS the new-user
# path and should be tested as one. `INJECT_CFG=1` opts in when you just
# don't want to retype the address.
deploy-c64u-disk-80-free: INJECT_CFG ?= 0
deploy-c64u-disk-80-free:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT) MODE80=1
	$(MAKE) -C c64_client disk-free
	@if [ "$(INJECT_CFG)" = "1" ]; then \
	   $(MAKE) inject-cfg CFG_DISK=c64_client/build/llm64-free.d64; \
	 else echo "cfg-free: first boot opens the config editor (the new-user path)"; fi
	$(PYTHON) emu/u64_telnet.py c64_client/build/llm64-free.d64 $(C64U_IP)

# Same disk, built DIAG=1 (crash post-mortem block at $02A7, see
# docs/07-crash-postmortem.md) and with a config already on it, so it
# boots straight to the chat instead of the first-run config editor.
C64_CFG_HOST ?= $(C64_PROXY_IP)
C64_CFG_PORT ?= $(C64_PROXY_PORT)

deploy-c64u-disk-80-diag:
	$(MAKE) -C c64_client clean
	$(MAKE) -C c64_client CONNECT=hayes SERVER_IP=$(C64_PROXY_IP) SERVER_PORT=$(C64_PROXY_PORT) MODE80=1 DIAG=1
	$(MAKE) -C c64_client disk
	@# NetConfig blob: 2 dummy bytes for the PRG header cbm_load skips,
	@# magic C6 01, then host[32] and port[6], NUL-padded. Digits and
	@# dots are identical in PETSCII and ASCII, so no conversion needed.
	$(PYTHON) -c "open('c64_client/build/user.cfg','wb').write(b'\x00\x10\xc6\x01'+b'$(C64_CFG_HOST)'.ljust(32,b'\0')+b'$(C64_CFG_PORT)'.ljust(6,b'\0'))"
	$(VICE_RUN) c1541 c64_client/build/llm64.d64 -write c64_client/build/user.cfg llm64.cfg
	$(PYTHON) emu/u64_telnet.py c64_client/build/llm64.d64 $(C64U_IP)
