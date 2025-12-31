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

# Compiler flags
CFLAGS = -t c64 -O -Or -Cl -I include
ASFLAGS = -t c64 -I include
LDFLAGS = -t c64 -m $(TARGETDIR)/$(TARGET).map

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

# Compile C files
$(TARGETDIR)/%.o: src/%.c
	@echo "Compiling $<..."
	CC65_HOME=$(CC65_HOME) $(CC) $(CFLAGS) -c $< -o $@

# Assemble ASM files
$(TARGETDIR)/%.o: src/%.s
	@echo "Assembling $<..."
	CC65_HOME=$(CC65_HOME) $(AS) $(ASFLAGS) -o $@ $<

# Clean
clean:
	@echo "Cleaning..."
	rm -f $(TARGETDIR)/*.o $(TARGETDIR)/*.map $(PRG)
	@echo "Clean complete"

# Run in VICE emulator (if available)
run: $(PRG)
	@if command -v x64sc >/dev/null 2>&1; then \
		x64sc $(PRG); \
	else \
		echo "VICE emulator (x64sc) not found"; \
	fi

# Show info
info:
	@echo "Target: $(TARGET)"
	@echo "C sources: $(C_SOURCES)"
	@echo "ASM sources: $(ASM_SOURCES)"
	@echo "Objects: $(OBJECTS)"

.PHONY: all clean run info
