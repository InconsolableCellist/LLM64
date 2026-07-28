# Shareware intro: a standalone PRG plus its data blob. Self-contained on
# purpose - it shares ../build/ with the client but touches nothing in the
# parent Makefile, so it can be rebuilt or deleted independently.

CC65_HOME = /usr/share/cc65
AS = ca65
LD = ld65
BUILD = ../build

# The tune is OURS - tune/llm64_theme.s, a small three-voice player, so
# the repository ships no music anybody else owns (every HVSC tune is
# copyrighted by its composer). Rebuild it, then the blob around it:
#
#   python3 ../../tools/make_intro_tune.py           # -> assets/llm64_theme.sid
#   python3 ../../tools/make_intro_assets.py \
#           assets/llm64_theme.sid -o assets/
#
# The outputs are committed, so a plain build needs neither PIL nor cc65's
# assembler for the tune. `make tune` does both steps.
ASSETS = assets/intro_data.bin assets/intro_gen.inc
TUNE = assets/llm64_theme.sid

all: $(BUILD)/intro.prg $(BUILD)/intro_data.bin

$(BUILD)/intro.prg: intro.s intro.cfg assets/intro_gen.inc | $(BUILD)
	CC65_HOME=$(CC65_HOME) $(AS) -t c64 -I assets intro.s -o $(BUILD)/intro.o
	CC65_HOME=$(CC65_HOME) $(LD) -C intro.cfg -m $(BUILD)/intro.map \
	    -o $@ $(BUILD)/intro.o

$(BUILD)/intro_data.bin: assets/intro_data.bin | $(BUILD)
	cp $< $@

$(BUILD):
	mkdir -p $(BUILD)

# Recompose the tune and rebuild the blob that carries it. Needs cc65
# (ca65/ld65) and Pillow; the results are committed, so this is only for
# when the score or the logo changes.
tune:
	python3 ../../tools/make_intro_tune.py -o $(TUNE)
	python3 ../../tools/make_intro_assets.py $(TUNE) -o assets/

clean:
	rm -f $(BUILD)/intro.o $(BUILD)/intro.map $(BUILD)/intro.prg \
	      $(BUILD)/intro_data.bin

.PHONY: all clean tune
