# LLM64 for Windows - 16-bit build (Open Watcom, hosted on Linux)
#
#   make            -> build/LLM64.EXE   (NE binary, Windows 3.x)
#   make run        -> build and launch it under Wine
#   make devproxy   -> mock LLM + proxy on a scratch port (see tools/)
#   make test       -> host-compiled unit test of the wire protocol
#   make floppy     -> build/llm64.img, a 1.44 MB disk with the EXE + INI
#   make vm-in      -> rebuild the floppy and insert it in a running VM
#   make vm-out     -> eject it
#
# `floppy` needs mtools (mformat/mcopy) - the one dependency here that
# is not the compiler. It is packaged everywhere: pacman -S mtools,
# apt install mtools, dnf install mtools.
#
# Open Watcom is not packaged on most distributions. Fetch the snapshot
# and point WATCOM at it:
#
#   mkdir -p ~/Programs && cd ~/Programs
#   curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
#   mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2

# Take the first snapshot that actually exists, so a machine that
# unpacked it somewhere else does not need WATCOM= on every invocation.
# The documented path is last as the fallback, so when nothing is
# installed the compiler-not-found error still names the place to put it.
WATCOM  ?= $(firstword $(wildcard $(HOME)/Programs/open-watcom-v2 \
                                  $(HOME)/opt/open-watcom-v2) \
                       $(HOME)/Programs/open-watcom-v2)
OWBIN   := $(WATCOM)/binl64
export WATCOM
# wcl shells out to wcc and wlink by name, so they have to be on PATH
export PATH := $(OWBIN):$(PATH)
export INCLUDE := $(WATCOM)/h/win:$(WATCOM)/h
export EDPATH  := $(WATCOM)/eddat

SRC     := src/wire.c src/scroll.c src/net.c src/main.c
OBJ     := $(patsubst src/%.c,build/%.obj,$(SRC))
EXE     := build/LLM64.EXE
RC      := src/llm64.rc

# The 32-bit PE, for Windows 10/11 and for Wine without the 16-bit
# subsystem. Same sources, same resources: include/llmport.h is the only
# file that knows which target it is, and it decides on __WATCOMC__.
#
# 32-bit rather than 64-bit on purpose - see llmport.h. WoW64 is on every
# x64 Windows and Windows 11 on ARM emulates x86-32, so this runs
# everywhere a 64-bit build would, with a much smaller diff.
W32CC   := i686-w64-mingw32-gcc
W32RC   := i686-w64-mingw32-windres
W32OBJ  := $(patsubst src/%.c,build/w32/%.o,$(SRC))
EXE32   := build/LLM32.EXE
# -mwindows picks the GUI subsystem, so no console appears behind it.
W32CFLAGS := -std=gnu89 -O2 -Wall -Iinclude -mwindows
W32LIBS   := -lcomdlg32 -lwinmm -lws2_32 -lgdi32 -luser32 -lkernel32

# -bt=windows  16-bit Windows target      -ml  large model (far code+data)
# -zW          Windows API prologue        -zu  DS != SS, as callbacks need
# -s           no stack-overflow checks (no runtime to report them to)
CFLAGS  := -q -bt=windows -ml -zW -zu -s -wx -Iinclude
# wcl drives the link too: it knows which runtime and import libraries
# a 16-bit Windows target needs, which wlink would have to be told.
LFLAGS  := -q -bt=windows -ml -l=windows

WINEPREFIX ?= $(HOME)/.wine-llm64
HOST ?= 127.0.0.1
PORT ?= 6400

# A 1.44 MB floppy image is how the binary gets into a VM or onto real
# hardware. QEMU's user-mode networking puts the host at 10.0.2.2 as
# seen from the guest, so that is what the INI on it points at.
#
# A proxy on another box is reached by its LAN address instead - slirp
# NATs out through the host, so the guest gets wherever the host can go:
#   make floppy VMHOST=192.168.1.21 VMPORT=6400
VMHOST ?= 10.0.2.2
VMPORT ?= 6410

.PHONY: all both run run32 spike spike16 mdispike mdispike16 spike-floppy run-spike devproxy test floppy vm-in vm-out clean

all: $(EXE)

# Both targets in one go. Use this rather than `make`: the whole point of
# llmport.h is that the 16-bit build cannot be allowed to rot, and it
# only stays green if you compile it every time.
both: $(EXE) $(EXE32)

build:
	@mkdir -p build

build/w32:
	@mkdir -p build/w32

build/w32/%.o: src/%.c | build/w32
	$(W32CC) $(W32CFLAGS) -c -o $@ $<

$(EXE32): $(W32OBJ) $(RC) | build/w32
	$(W32RC) -i $(RC) -o build/w32/llm64_res.o --include-dir include
	$(W32CC) $(W32CFLAGS) -o $@ $(W32OBJ) build/w32/llm64_res.o $(W32LIBS)
	@echo "-> $@"

run32: $(EXE32)
	WINEPREFIX=$(WINEPREFIX) WINEDEBUG=-all wine $(EXE32) $(HOST) $(PORT)

# Tier 2 spike: the 3.1 frame, drawn by us. Standalone on purpose - see
# the header of spike/caption.c. Win32 only; there is nothing to prove on
# the 16-bit target, where the OS already draws this correctly.
spike: build/CAPSPIKE.EXE

build/CAPSPIKE.EXE: spike/caption.c src/chrome.c | build/w32
	$(W32CC) $(W32CFLAGS) -o $@ spike/caption.c src/chrome.c -lgdi32 -luser32
	@echo "-> $@"

run-spike: build/CAPSPIKE.EXE
	WINEPREFIX=$(WINEPREFIX) WINEDEBUG=-all wine build/CAPSPIKE.EXE

# The SAME spike source as a 16-bit NE. This is the claim that the chrome
# is target-neutral, made checkable: if it builds and runs here, the 3.1
# look can go on Windows 95 and 98 too, where the OS would otherwise dress
# the app as 1995.
spike16: build/CAPSPK16.EXE

build/CAPSPK16.EXE: spike/caption.c src/chrome.c | build
	$(OWBIN)/wcl $(CFLAGS) $(LFLAGS) -fe=$@ spike/caption.c src/chrome.c
	@rm -f __wcl_*.lnk caption.obj chrome.obj
	@echo "-> $@"

# The MDI experiment: can a child give up its non-client area and keep
# DefMDIChildProc? See the header of spike/mdi.c.
mdispike: build/MDISPIKE.EXE
mdispike16: build/MDISPK16.EXE

build/MDISPIKE.EXE: spike/mdi.c src/chrome.c | build/w32
	$(W32CC) $(W32CFLAGS) -o $@ spike/mdi.c src/chrome.c -lgdi32 -luser32
	@echo "-> $@"

build/MDISPK16.EXE: spike/mdi.c src/chrome.c | build
	$(OWBIN)/wcl $(CFLAGS) $(LFLAGS) -fe=$@ spike/mdi.c src/chrome.c
	@rm -f __wcl_*.lnk mdi.obj chrome.obj
	@echo "-> $@"

# A floppy carrying the 16-bit spike into a real 3.11 or 95 guest - and
# the screenshots back out, since the EXE is 8 KB and the disk is 1.44 MB.
# Only the NE build goes on it: CAPSPIKE.EXE is a 32-bit PE, which 3.11
# cannot run at all and 95 cannot either (it imports the UCRT).
#
#   make spike-floppy
#   ./tools/vmfloppy.sh in build/spike.img     (running guest)
#   qemu ... -fda build/spike.img              (at boot)
spike-floppy: build/CAPSPK16.EXE
	rm -f build/spike.img
	mformat -C -f 1440 -v SPIKE -i build/spike.img ::
	mcopy -i build/spike.img build/CAPSPK16.EXE ::/CAPSPK16.EXE
	printf '%s\r\n' \
	  'CAPSPK16.EXE - the Windows 3.1 chrome, drawn by the program.' \
	  '' \
	  'Run it:        A:\CAPSPK16.EXE          (a normal window)' \
	  '               A:\CAPSPK16.EXE max      (starts maximised)' \
	  '' \
	  'Captures wanted, in order of value:' \
	  '' \
	  ' 1. UNMAXIMISED, beside a real 3.11 window (Notepad will do),' \
	  '    both visible at once. The reference capture we have is' \
	  '    maximised, and a maximised 3.1 window has no sizing border,' \
	  '    so the border is the one thing that cannot be checked.' \
	  '' \
	  ' 2. MENUS OPEN - Notepad Edit menu (it has greyed items) and' \
	  '    this program File menu. Every popup metric here is guessed;' \
	  '    none of it is measured.' \
	  '' \
	  ' 3. MAXIMISED, to compare against what we already have.' \
	  '' \
	  'How: PrintScreen grabs the screen, Alt+PrintScreen just the' \
	  'active window. Then PBRUSH.EXE, and BEFORE pasting set' \
	  'Options > Image Attributes to the screen size in pels, or the' \
	  'paste is cropped to the default canvas. Edit > Paste, then' \
	  'File > Save As to A: as PCX - a 1024x768 BMP is ~790 KB and' \
	  'only one will fit; PCX is run-length coded and flat UI shrinks' \
	  'to a fraction of that.' \
	  > build/SPIKE.TXT
	mcopy -i build/spike.img build/SPIKE.TXT ::/README.TXT
	@echo
	@mdir -i build/spike.img :: | tail -4
	@echo "-> $(CURDIR)/build/spike.img"

build/%.obj: src/%.c | build
	$(OWBIN)/wcc $(CFLAGS) -fo=$@ $<

$(EXE): $(OBJ) $(RC) | build
	$(OWBIN)/wcl $(LFLAGS) -fe=$@ $(OBJ) winsock.lib commdlg.lib mmsystem.lib
	$(OWBIN)/wrc -q -bt=windows -i=$(WATCOM)/h/win -i=include -fo=build/llm64.res $(RC) $@
	@rm -f __wcl_*.lnk

run: $(EXE)
	WINEPREFIX=$(WINEPREFIX) WINEDEBUG=-all wine $(EXE) $(HOST) $(PORT)

devproxy:
	./tools/devproxy.sh

# The framing and the transcript are plain C with no Windows in them, so
# they can be tested on the host at native speed - no emulator, no Wine.
test:
	@mkdir -p build
	cc -Wall -Iinclude -o build/test_wire src/wire.c tests/test_wire.c
	./build/test_wire
	cc -Wall -Iinclude -o build/test_scroll src/scroll.c tests/test_scroll.c
	./build/test_scroll

# The EXE plus an INI, on a floppy image a VM can mount. The INI has to
# travel with it: the client reads the one beside itself, and CRLF is
# not optional in a file Windows will parse.
floppy: $(EXE)
	rm -f build/llm64.img
	mformat -C -f 1440 -v LLM64 -i build/llm64.img ::
	printf '[Server]\r\nHost=%s\r\nPort=%s\r\n' '$(VMHOST)' '$(VMPORT)' \
	    > build/LLM64.INI
	mcopy -i build/llm64.img $(EXE) ::/LLM64.EXE
	mcopy -i build/llm64.img build/LLM64.INI ::/LLM64.INI
	@echo
	@echo "-> $(CURDIR)/build/llm64.img  (A: = $(VMHOST):$(VMPORT))"
	@echo "   attach to QEMU with:  -fda $(CURDIR)/build/llm64.img"

# ...or hand it to a VM that is already up, which is the actual test
# loop: edit, `make vm-in`, and the guest sees the new build without
# leaving Windows. tools/vmfloppy.sh explains why it ejects first.
vm-in: floppy
	./tools/vmfloppy.sh in

vm-out:
	./tools/vmfloppy.sh out

clean:
	rm -rf build
