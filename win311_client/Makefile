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

.PHONY: all run devproxy test floppy vm-in vm-out clean

all: $(EXE)

build:
	@mkdir -p build

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
