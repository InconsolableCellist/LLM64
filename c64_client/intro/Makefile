# Shareware intro: a standalone PRG plus its data blob. Self-contained on
# purpose - it shares ../build/ with the client but touches nothing in the
# parent Makefile, so it can be rebuilt or deleted independently.

CC65_HOME = /usr/share/cc65
AS = ca65
LD = ld65
BUILD = ../build

# Regenerate assets/ (the drawn logo plus the tune) with:
#   python3 ../../tools/make_intro_assets.py \
#           ../../c64llm_proxy/data/sids/b000_full/<tune>.sid -o assets/
# The outputs are committed, so a plain build needs neither PIL nor the
# SID library.
ASSETS = assets/intro_data.bin assets/intro_gen.inc

all: $(BUILD)/intro.prg $(BUILD)/intro_data.bin

$(BUILD)/intro.prg: intro.s intro.cfg assets/intro_gen.inc | $(BUILD)
	CC65_HOME=$(CC65_HOME) $(AS) -t c64 -I assets intro.s -o $(BUILD)/intro.o
	CC65_HOME=$(CC65_HOME) $(LD) -C intro.cfg -m $(BUILD)/intro.map \
	    -o $@ $(BUILD)/intro.o

$(BUILD)/intro_data.bin: assets/intro_data.bin | $(BUILD)
	cp $< $@

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -f $(BUILD)/intro.o $(BUILD)/intro.map $(BUILD)/intro.prg \
	      $(BUILD)/intro_data.bin

.PHONY: all clean
