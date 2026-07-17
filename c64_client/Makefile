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

# Upload to C64 via FTP
C64_IP = 192.168.1.64
C64_PATH = /Temp

upload: $(PRG)
	@echo "Uploading $(PRG) to C64 at $(C64_IP)..."
	@curl -T $(PRG) ftp://$(C64_IP)$(C64_PATH)/$(TARGET).prg --user anonymous: || \
		(echo "Upload failed! Make sure C64 FTP server is running at $(C64_IP)" && exit 1)
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

.PHONY: all clean run upload deploy info
