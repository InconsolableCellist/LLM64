# C64 LLM Client Makefile

# Tools
CC = cl65
AS = ca65
LD = ld65

# Set CC65_HOME explicitly before running commands
CC65_HOME = /usr/share/cc65

# Target
TARGET = c64llm
TARGETDIR = build
PRG = $(TARGETDIR)/$(TARGET).prg

# Connection mode: hayes (AT dial, real C64 Ultimate) or direct (raw pipe, VICE)
CONNECT ?= hayes
# Proxy server address baked into the binary
SERVER_IP ?= 192.168.1.39
SERVER_PORT ?= 6400

# Compiler flags
CFLAGS = -t c64 -O -Or -Cl -I include
CFLAGS += -DSERVER_IP=\"$(SERVER_IP)\" -DSERVER_PORT=\"$(SERVER_PORT)\"
ifdef TEST_MESSAGE
CFLAGS += -DTEST_MESSAGE='"$(TEST_MESSAGE)"'
endif
# DEBUG_CLIENT=1 builds the scripted diagnostic session instead of the TUI
ifdef DEBUG_CLIENT
CFLAGS += -DDEBUG_CLIENT
endif
ifeq ($(CONNECT),direct)
CFLAGS += -DCONNECT_DIRECT
endif
CFLAGS += $(CFLAGS_EXTRA)
ASFLAGS = -t c64 -I include

# Git hash shown in the title bar; '+' marks a dirty working tree.
GIT_HASH := $(shell git rev-parse --short=7 HEAD 2>/dev/null || echo nogit)
ifneq ($(strip $(shell git status --porcelain 2>/dev/null)),)
GIT_HASH := $(GIT_HASH)+
endif
HASHFILE = include/buildhash.h
LDFLAGS = -t c64 -m $(TARGETDIR)/$(TARGET).map -Ln $(TARGETDIR)/labels.txt

# MODE80=1 builds the soft-80-column bitmap UI instead of 40-column text.
# Custom linker config caps the program at $C000: above it live the ASCII
# shadow ($C000), the color matrix ($CC00) and (under the KERNAL ROM) the
# bitmap ($E000); the default config would put the C stack on top of them.
ifdef MODE80
CFLAGS += -DSOFT80
ASFLAGS += -D SOFT80
LDFLAGS += -C c64-soft80.cfg
endif

# Source files
C_SOURCES = $(wildcard src/*.c)
ASM_SOURCES = $(wildcard src/*.s)

# Object files
C_OBJECTS = $(C_SOURCES:src/%.c=$(TARGETDIR)/%.o)
ASM_OBJECTS = $(ASM_SOURCES:src/%.s=$(TARGETDIR)/%.o)
OBJECTS = $(C_OBJECTS) $(ASM_OBJECTS)

# Default target
all: $(PRG)

# Link
$(PRG): $(OBJECTS)
	@echo "Linking $(PRG)..."
	CC65_HOME=$(CC65_HOME) $(CC) $(LDFLAGS) -o $@ $^
	@echo "Build complete: $(PRG)"
	@ls -lh $(PRG)

$(OBJECTS): | $(TARGETDIR)

$(TARGETDIR):
	mkdir -p $(TARGETDIR)

# buildhash.h is only rewritten when the hash actually changes, so
# incremental builds don't recompile display.c every time.
$(HASHFILE): FORCE
	@printf '#define GIT_HASH "%s"\n' '$(GIT_HASH)' > $(HASHFILE).tmp; \
	if cmp -s $(HASHFILE).tmp $(HASHFILE); then rm -f $(HASHFILE).tmp; \
	else mv $(HASHFILE).tmp $(HASHFILE); fi

FORCE:

$(TARGETDIR)/display.o: $(HASHFILE)

# Compile C files (compile to .s then assemble to .o)
$(TARGETDIR)/%.o: src/%.c
	@echo "Compiling $<..."
	CC65_HOME=$(CC65_HOME) cc65 $(CFLAGS) $< -o $(TARGETDIR)/$*.s
	@echo "Assembling $*.s..."
	CC65_HOME=$(CC65_HOME) $(AS) $(ASFLAGS) -o $@ $(TARGETDIR)/$*.s

# Assemble ASM files
$(TARGETDIR)/%.o: src/%.s
	@echo "Assembling $<..."
	CC65_HOME=$(CC65_HOME) $(AS) $(ASFLAGS) -o $@ $<

# Overlay modules + boot disk (MODE80 builds emit $(PRG).1, .2, ...)
MOD1 = $(PRG).1
MOD2 = $(PRG).2
D64 = $(TARGETDIR)/c64llm.d64

# Bootable distribution disk: client PRG first (LOAD"*",8,1 / the
# Ultimate's Run Disk boots it), then the overlay module. No config
# file on a fresh disk - first boot opens the editor and the save
# lands in the image (the C64U writes mounted d64s through), so the
# disk carries its own settings from then on. Burnable to real media.
disk: $(D64)
$(D64): $(PRG)
	@test -f $(MOD1) || { echo "No $(MOD1) - build with MODE80=1"; exit 1; }
	@c1541 -format "c64llm,01" d64 $(D64) \
	       -write $(PRG) "c64llm" \
	       -write $(MOD1) "c64llm.1" \
	       -write $(MOD2) "c64llm.2" >/dev/null
	@echo "Disk image: $(D64)"

# Clean
clean:
	@echo "Cleaning..."
	rm -f $(TARGETDIR)/*.o $(TARGETDIR)/*.map $(PRG) $(MOD1) $(MOD2) $(D64)
	@echo "Clean complete"

# Run in VICE emulator (if available)
run: $(PRG)
	@if command -v x64sc >/dev/null 2>&1; then \
		x64sc $(PRG); \
	else \
		echo "VICE emulator (x64sc) not found"; \
	fi

# Upload to C64 via FTP
C64_IP = 192.168.1.64
C64_PATH = /Temp

upload: $(PRG)
	@echo "Uploading $(PRG) to C64 at $(C64_IP)..."
	@curl -T $(PRG) ftp://$(C64_IP)$(C64_PATH)/$(TARGET).prg --user anonymous: || \
		(echo "Upload failed! Make sure C64 FTP server is running at $(C64_IP)" && exit 1)
	@for m in $(MOD1) $(MOD2); do \
		[ -f $$m ] || continue; \
		ext=$${m##*.}; \
		curl -T $$m ftp://$(C64_IP)$(C64_PATH)/C64LLM.$$ext --user anonymous: && \
		echo "Module uploaded: C64LLM.$$ext"; \
	done
	@echo "Upload complete: ftp://$(C64_IP)$(C64_PATH)/$(TARGET).prg"

# Build and upload in one step
deploy: clean all upload
	@echo "Build and deployment complete!"

# Show info
info:
	@echo "Target: $(TARGET)"
	@echo "C sources: $(C_SOURCES)"
	@echo "ASM sources: $(ASM_SOURCES)"
	@echo "Objects: $(OBJECTS)"
	@echo "C64 FTP: ftp://$(C64_IP)$(C64_PATH)/"

.PHONY: all clean run upload deploy info disk
