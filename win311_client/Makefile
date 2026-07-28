# LLM64 for Windows - 16-bit build (Open Watcom, hosted on Linux)
#
#   make            -> build/LLM64.EXE   (NE binary, Windows 3.x)
#   make run        -> build and launch it under Wine
#   make devproxy   -> mock LLM + proxy on a scratch port (see tools/)
#   make test       -> host-compiled unit test of the wire protocol
#
# Open Watcom is not packaged on most distributions. Fetch the snapshot
# and point WATCOM at it:
#
#   mkdir -p ~/Programs && cd ~/Programs
#   curl -LO https://github.com/open-watcom/open-watcom-v2/releases/download/Current-build/ow-snapshot.tar.xz
#   mkdir open-watcom-v2 && tar -xf ow-snapshot.tar.xz -C open-watcom-v2

WATCOM  ?= $(HOME)/Programs/open-watcom-v2
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

.PHONY: all run devproxy test clean

all: $(EXE)

build:
	@mkdir -p build

build/%.obj: src/%.c | build
	$(OWBIN)/wcc $(CFLAGS) -fo=$@ $<

$(EXE): $(OBJ) $(RC) | build
	$(OWBIN)/wcl $(LFLAGS) -fe=$@ $(OBJ) winsock.lib
	$(OWBIN)/wrc -q -bt=windows -i=$(WATCOM)/h/win -fo=build/llm64.res $(RC) $@
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

clean:
	rm -rf build
