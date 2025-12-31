# C64 LLM Client - Technical Design Document

## Overview
A TUI application for the Commodore 64 that provides an interactive LLM chat interface with text editing, scrolling, conversation management, and responsive serial communication.

## Development Environment
- **Compiler**: cc65 (C compiler for 6502)
- **Language**: C for high-level logic, 6502 assembly for performance-critical sections
- **Build**: Makefile-based build system
- **Target**: C64 (.prg file, ~20-30KB expected)
- **Testing**: VICE emulator with virtual serial port

## Memory Map

### C64 Memory Layout (64KB total)
```
$0000-$00FF   Zero Page (256 bytes)
              - Used for cc65 runtime and our critical pointers
$0100-$01FF   Stack (256 bytes)
$0200-$03FF   Reserved/BASIC
$0400-$07FF   Screen RAM (1000 bytes, 40x25)
$0800-$0FFF   Application code start (~2KB usable)
$1000-$1FFF   Application code continued
$2000-$3FFF   Application data/buffers (~8KB)
$4000-$9FFF   Application code/data (~24KB available)
$A000-$BFFF   BASIC ROM (banked out for our use: +8KB)
$C000-$CFFF   RAM (4KB)
$D000-$DFFF   I/O area (VIC-II, SID, CIA chips)
$E000-$FFFF   KERNAL ROM (keep for IRQ handling)
```

### Application Memory Layout
```
$0801-$1FFF   Main application code (~6KB)
$2000-$23FF   Screen buffer (1024 bytes, double-buffer for flicker-free)
$2400-$27FF   Color RAM shadow (1024 bytes)
$2800-$2BFF   Text edit buffer (1024 bytes max message)
$2C00-$2FFF   Serial RX buffer (512 bytes circular)
$3000-$30FF   Serial TX buffer (256 bytes circular)
$3100-$33FF   Current conversation buffer (~768 bytes, ring buffer)
$3400-$37FF   Conversation list cache (1KB, ~20 entries)
$3800-$3FFF   Temporary/scratch buffers (2KB)
$4000-$9FFF   Extended code/data if needed
```

### Zero Page Allocations (Critical Fast-Access Variables)
```
$02-$03   Screen pointer (current write position)
$04-$05   Edit buffer pointer
$06-$07   RX buffer head pointer
$08-$09   RX buffer tail pointer
$0A-$0B   TX buffer head pointer
$0C-$0D   TX buffer tail pointer
$0E       Current UI state
$0F       Serial state flags
$10-$11   Conversation buffer pointer
$12       Scroll offset
$13       Cursor position in edit buffer
$14-$15   Temp pointer for memory operations
...
(cc65 reserves some zero page locations)
```

## Screen Layout

### 40x25 Character Display (PETSCII)
```
┌────────────────────────────────────────┐ Row 0
│ ┌─────────────────────────────────┐    │ Row 1
│ │                                 │    │
│ │     Chat History Area           │    │
│ │     (Scrollable)                │    │ Rows 1-19
│ │                                 │    │ (19 lines)
│ │     User: Hello                 │    │
│ │     AI: Hi there! How can I...  │    │
│ │                                 │    │
│ └─────────────────────────────────┘    │ Row 19
│ ────────────────────────────────────── │ Row 20 (separator)
│ Edit:                                  │ Row 21 (label)
│ ┌────────────────────────────────────┐ │ Row 22
│ │ User types here_                   │ │ Row 22-24
│ └────────────────────────────────────┘ │ (3 lines edit)
│ F1:Send F3:Cancel F5:Conv F7:Help     │ Row 24
└────────────────────────────────────────┘

Sidebar view (when active):
┌────────────────┬───────────────────────┐
│ Conversations  │  Chat Area            │
│ ────────────── │                       │
│ > Today        │  (Chat continues)     │
│   BASIC help   │                       │
│   Yesterday    │                       │
│   6502 coding  │                       │
│   Debugging    │                       │
│                │                       │
│ F1:Load        │                       │
│ F5:Close       │                       │
└────────────────┴───────────────────────┘
   15 cols            25 cols
```

### Color Scheme
- Background: Light Blue ($0E)
- Border: Dark Blue ($06)
- Text: Dark Blue ($06) on Light Blue
- User messages: White ($01)
- Assistant messages: Light Green ($0D)
- System messages: Yellow ($07)
- Edit area: White ($01) on Dark Blue ($06)
- Status bar: White on Dark Gray

## Module Architecture

### Main Modules

```
main.c
├── ui.c/.h           - UI rendering and layout
├── editor.c/.h       - Text editor logic
├── display.c/.h      - Chat display and scrolling
├── serial.c/.h       - Serial I/O layer (mostly ASM)
├── protocol.c/.h     - Protocol message handling
├── conversation.c/.h - Conversation management
├── input.c/.h        - Keyboard input handling
└── util.c/.h         - Utility functions (memory, strings)

serial.s              - Assembly serial routines
charset.s             - Custom character set data (optional)
```

## Module Specifications

### 1. main.c - Application Entry and Main Loop

```c
// Main application state
typedef enum {
    STATE_CHAT,          // Normal chat view
    STATE_SIDEBAR,       // Conversation selector visible
    STATE_HELP,          // Help screen
    STATE_STATUS         // Showing status message
} AppState;

typedef struct {
    AppState state;
    uint8_t running;
    uint8_t dirty_screen;    // Needs redraw
    uint8_t dirty_edit;      // Edit area needs redraw
    uint8_t dirty_chat;      // Chat area needs redraw
    uint32_t current_conv_id;
    uint8_t request_pending; // Waiting for LLM response
} AppContext;

// Main loop (60Hz iteration target)
void main_loop(void) {
    while (app.running) {
        // 1. Poll serial port (non-blocking)
        serial_poll();

        // 2. Process any received messages
        protocol_process();

        // 3. Scan keyboard
        input_scan();

        // 4. Update UI (only dirty areas)
        ui_update();

        // 5. Brief delay to ~60Hz
        // (Could use raster IRQ for precise timing)
    }
}
```

**Functions:**
- `void main(void)` - Entry point, initialization
- `void init_system(void)` - Hardware setup, bank switching
- `void init_app(void)` - Application state initialization
- `void main_loop(void)` - Main event loop
- `void shutdown(void)` - Cleanup and exit

### 2. ui.c - User Interface Rendering

**Responsibilities:**
- Screen layout and rendering
- Color management
- Status bar updates
- Viewport management

**Functions:**
- `void ui_init(void)` - Initialize screen, colors, layout
- `void ui_update(void)` - Render dirty areas only
- `void ui_render_frame(void)` - Draw static UI frame
- `void ui_render_statusbar(void)` - Update status bar
- `void ui_show_help(void)` - Display help screen
- `void ui_show_status(const char* msg)` - Temporary status message
- `void ui_clear_area(uint8_t x, uint8_t y, uint8_t w, uint8_t h)`
- `void ui_set_color(uint8_t x, uint8_t y, uint8_t color)`

### 3. editor.c - Text Editor Component

**Responsibilities:**
- Text buffer management (max 1024 bytes)
- Cursor movement and positioning
- Character insertion/deletion
- Multi-line text wrapping
- Emacs-style keybindings

**Data Structures:**
```c
typedef struct {
    char buffer[1024];     // Edit buffer
    uint16_t length;       // Current text length
    uint16_t cursor_pos;   // Cursor position in buffer
    uint8_t cursor_x;      // Screen X position
    uint8_t cursor_y;      // Screen Y position (relative to edit area)
    uint8_t scroll_offset; // For horizontal scrolling if needed
    uint8_t dirty;         // Needs redraw
} Editor;
```

**Functions:**
- `void editor_init(Editor* ed)` - Initialize editor
- `void editor_clear(Editor* ed)` - Clear buffer
- `void editor_insert_char(Editor* ed, char c)` - Insert at cursor
- `void editor_delete_char(Editor* ed)` - Delete at cursor (backspace)
- `void editor_delete_to_end(Editor* ed)` - Ctrl-K: Kill to end of line
- `void editor_cursor_home(Editor* ed)` - Ctrl-A: Beginning of line
- `void editor_cursor_end(Editor* ed)` - Ctrl-E: End of line
- `void editor_cursor_left(Editor* ed)` - Move cursor left
- `void editor_cursor_right(Editor* ed)` - Move cursor right
- `void editor_get_text(Editor* ed, char* dest)` - Copy buffer to dest
- `void editor_render(Editor* ed)` - Render to screen
- `void editor_update_cursor(Editor* ed)` - Update cursor position

**Emacs Keybindings:**
- Ctrl-A: Beginning of line
- Ctrl-E: End of line
- Ctrl-K: Kill to end of line
- Ctrl-D: Delete character at cursor
- Cursor keys: Navigate
- Backspace/Delete: Delete character

### 4. display.c - Chat Display and Scrolling

**Responsibilities:**
- Render chat messages in scrollable viewport
- Manage conversation ring buffer
- Handle word wrapping
- Scroll up/down through history
- Incremental text display (streaming)

**Data Structures:**
```c
typedef enum {
    MSG_USER,
    MSG_ASSISTANT,
    MSG_SYSTEM
} MessageRole;

typedef struct {
    MessageRole role;
    uint16_t length;
    char text[512];        // Max message chunk size
} Message;

typedef struct {
    Message messages[16];  // Ring buffer of messages
    uint8_t head;          // Write position
    uint8_t tail;          // Read position
    uint8_t count;         // Number of messages
    uint8_t scroll_pos;    // Current scroll position
    uint8_t display_lines; // Total lines to display (computed)
    uint8_t dirty;         // Needs redraw
} ChatDisplay;
```

**Functions:**
- `void display_init(ChatDisplay* disp)` - Initialize display
- `void display_add_message(ChatDisplay* disp, MessageRole role, const char* text)` - Add complete message
- `void display_append_chunk(ChatDisplay* disp, const char* chunk)` - Append to last message (streaming)
- `void display_scroll_up(ChatDisplay* disp, uint8_t lines)` - Scroll up
- `void display_scroll_down(ChatDisplay* disp, uint8_t lines)` - Scroll down
- `void display_render(ChatDisplay* disp)` - Render visible portion
- `void display_clear(ChatDisplay* disp)` - Clear all messages
- `uint8_t display_wrap_text(const char* text, uint8_t width, char lines[][40], uint8_t max_lines)` - Word wrap utility

**Scrolling Logic:**
- Display viewport: 19 lines
- Compute total wrapped lines for all messages
- Scroll position indicates top line to display
- Cursor Up/Down: Scroll by 1 line
- Page Up/Down: Scroll by viewport height

### 5. serial.c / serial.s - Serial Communication Layer

**Responsibilities:**
- Low-level UART communication (User Port)
- Non-blocking TX/RX
- Circular buffer management
- XON/XOFF flow control
- Baud rate configuration

**Implementation Notes:**
- Use CIA #2 ($DD00) for User Port bit-banging OR
- Use 6551 ACIA if available (faster, hardware UART)
- Assembly implementation for speed
- IRQ-driven or polled (polled simpler for v1)

**C Interface:**
```c
// Initialize serial port
void serial_init(uint16_t baud);

// Check if data available to read
uint8_t serial_available(void);

// Non-blocking read (returns 0 if no data)
uint8_t serial_read(void);

// Non-blocking write (returns 0 if buffer full)
uint8_t serial_write(uint8_t byte);

// Write multiple bytes
uint16_t serial_write_buffer(const uint8_t* data, uint16_t len);

// Check if TX buffer has space
uint8_t serial_can_write(void);

// Flush TX buffer (blocking until sent)
void serial_flush(void);

// Poll serial port (call from main loop)
void serial_poll(void);
```

**Assembly Routines (serial.s):**
- `_serial_init`: Configure User Port pins, baud rate timer
- `_serial_tx_byte`: Transmit one byte (bit-banging)
- `_serial_rx_byte`: Receive one byte (bit-banging)
- `_serial_poll`: Check for incoming data, manage buffers

### 6. protocol.c - Protocol Message Handling

**Responsibilities:**
- Encode/decode protocol messages
- Frame synchronization
- CRC validation
- Message dispatch
- Request/response correlation

**Data Structures:**
```c
typedef enum {
    MSG_CHAT_REQUEST = 0x01,
    MSG_CANCEL_REQUEST = 0x02,
    MSG_LIST_CONVERSATIONS = 0x03,
    MSG_LOAD_CONVERSATION = 0x04,
    MSG_NEW_CONVERSATION = 0x05,
    MSG_PING = 0x06,
    MSG_ACK = 0x10,
    MSG_NAK = 0x11,
    MSG_CHAT_CHUNK = 0x20,
    MSG_CHAT_DONE = 0x21,
    MSG_CHAT_ERROR = 0x22,
    MSG_CONVERSATION_LIST = 0x23,
    MSG_CONVERSATION_DATA = 0x24,
    MSG_STATUS = 0x25
} MessageType;

typedef struct {
    MessageType type;
    uint16_t length;
    uint8_t payload[512];  // Max payload per message
} ProtocolMessage;

typedef enum {
    SYNC_SEARCHING,    // Looking for SYNC byte
    SYNC_FOUND,        // Got SYNC, reading header
    READING_PAYLOAD,   // Reading payload
    VALIDATING         // Checking CRC
} ProtocolState;
```

**Functions:**
- `void protocol_init(void)` - Initialize protocol handler
- `void protocol_process(void)` - Process incoming serial data
- `void protocol_send_message(MessageType type, const uint8_t* payload, uint16_t len)` - Send message
- `void protocol_send_chat(const char* text)` - Send chat request
- `void protocol_send_cancel(void)` - Send cancel request
- `void protocol_send_list_conversations(void)` - Request conversation list
- `void protocol_send_load_conversation(uint32_t id)` - Load conversation
- `uint8_t protocol_calculate_crc(const uint8_t* data, uint16_t len)` - Simple XOR CRC

**Message Handlers (callbacks):**
- `void on_chat_chunk(uint8_t seq, const char* text)` - Handle streaming chunk
- `void on_chat_done(uint8_t seq, uint16_t total_len)` - Response complete
- `void on_chat_error(const char* error)` - Error message
- `void on_conversation_list(ConversationInfo* convs, uint8_t count, uint8_t more)` - Conversation list
- `void on_status(const char* status)` - Status update

### 7. conversation.c - Conversation Management

**Responsibilities:**
- Cache conversation list from server
- Track current conversation
- New/load/switch conversations

**Data Structures:**
```c
typedef struct {
    uint32_t id;
    uint32_t timestamp;
    char title[40];        // Truncated for C64 display
} ConversationInfo;

typedef struct {
    ConversationInfo list[30];  // Cache up to 30 conversations
    uint8_t count;
    uint8_t selected;           // Currently selected in sidebar
    uint32_t current_id;        // Active conversation
    uint8_t dirty;              // List needs refresh
} ConversationManager;
```

**Functions:**
- `void conv_init(ConversationManager* mgr)` - Initialize
- `void conv_request_list(ConversationManager* mgr)` - Request from server
- `void conv_add_to_cache(ConversationManager* mgr, const ConversationInfo* info)` - Add to cache
- `void conv_select_next(ConversationManager* mgr)` - Move selection down
- `void conv_select_prev(ConversationManager* mgr)` - Move selection up
- `void conv_load_selected(ConversationManager* mgr)` - Load selected conversation
- `void conv_new(ConversationManager* mgr)` - Start new conversation
- `void conv_render_sidebar(ConversationManager* mgr)` - Render sidebar UI

### 8. input.c - Keyboard Input Handling

**Responsibilities:**
- Scan keyboard matrix
- Debounce keys
- Handle special key combinations
- F-key detection
- Ctrl key combinations

**Functions:**
- `void input_init(void)` - Initialize keyboard
- `void input_scan(void)` - Scan keyboard (call every frame)
- `uint8_t input_get_key(void)` - Get pressed key (PETSCII)
- `uint8_t input_check_fkey(void)` - Check F-keys (returns F1-F8 or 0)
- `uint8_t input_ctrl_pressed(void)` - Check if Ctrl held
- `uint8_t input_shift_pressed(void)` - Check if Shift held

**Key Mapping:**
- F1: Send message
- F3: Cancel request
- F5: Toggle conversation sidebar
- F7: Help screen
- Ctrl+A/E/K/D: Editor commands
- Cursor keys: Navigate
- Return: Newline in editor (or send if single-line mode)

## Text Editing Detailed Design

### Multi-line Editing (3 rows, 40 columns = 120 visible chars)

**Buffer Layout:**
- Linear buffer, wrap on display
- Cursor tracks position in buffer (0-1023)
- Screen coordinates computed from cursor position

**Insertion:**
1. Insert character at cursor position
2. Shift remaining buffer right
3. Increment length
4. Recompute screen coordinates
5. Mark editor dirty

**Deletion (Backspace):**
1. Delete character before cursor
2. Shift remaining buffer left
3. Decrement length
4. Recompute screen coordinates

**Ctrl-K (Kill to end of line):**
1. Find next newline or end of buffer
2. Delete all characters from cursor to newline
3. Preserve newline character

**Rendering:**
1. Clear edit area (3 rows)
2. Compute visible window (if buffer > 120 chars, show last 120)
3. Wrap text to 40 columns
4. Display up to 3 rows
5. Position cursor at correct screen coordinate
6. Flash cursor (toggle every ~30 frames)

**Word Wrap:**
- Break at spaces when possible
- If word > 40 chars, break mid-word

## Chat Display Detailed Design

### Scrollback Buffer Strategy

**Ring Buffer of Messages:**
- Fixed array of Message structs
- Each message up to 512 bytes
- When buffer full, oldest message overwritten
- Allows ~8-16 recent messages in memory

**Rendering Pipeline:**
1. Iterate messages from tail to head
2. Word-wrap each message to 40 columns
3. Accumulate total line count
4. Compute which lines are visible (scroll_pos to scroll_pos+19)
5. Render only visible lines
6. Prefix each message with role indicator:
   - `You:` for user
   - `AI:` for assistant
   - `***` for system messages

**Streaming Display:**
- When CHAT_CHUNK arrives, append to last assistant message
- Re-render affected lines (only last visible message typically)
- Auto-scroll to bottom unless user manually scrolled up

**Scroll Logic:**
```c
total_lines = compute_total_wrapped_lines();
max_scroll = max(0, total_lines - VIEWPORT_HEIGHT);
scroll_pos = clamp(scroll_pos, 0, max_scroll);

// On cursor up
scroll_pos = max(0, scroll_pos - 1);

// On cursor down
scroll_pos = min(max_scroll, scroll_pos + 1);
```

## State Machine and Responsiveness

### Main Loop State
```
Idle ──F1──> Sending Request ──ACK──> Waiting for Response
  │             │                         │
  │             └──timeout──> Error       ├──CHUNK──> Receiving
  │                            │          │            │
  │                            │          │            ├──auto-scroll
  F5                          Idle        │            │
  │                                       └──DONE──> Idle
  │
  └──> Sidebar View ──F5──> Idle
       │
       └──F1──> Loading Conversation ──DATA──> Idle
```

### Non-blocking Principles
- **Serial I/O**: Polled, never blocking
- **Keyboard**: Scanned every frame, immediate response
- **Rendering**: Only dirty areas, <1 frame time
- **Streaming**: Chunks displayed as they arrive

### F3 Cancel Handling
1. User presses F3 during response
2. Set `request_pending = 0`
3. Send CANCEL_REQUEST message
4. Display "Cancelled" status
5. Discard any subsequent CHAT_CHUNKs until CHAT_DONE received

## Build System

### Makefile Structure
```makefile
CC = cl65
CFLAGS = -t c64 -O -Or
AS = ca65
LD = ld65

OBJS = main.o ui.o editor.o display.o serial.o protocol.o conversation.o input.o util.o

c64llm.prg: $(OBJS)
	$(LD) -t c64 -o $@ $(OBJS) c64.lib

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

%.o: %.s
	$(AS) -t c64 $< -o $@

clean:
	rm -f *.o c64llm.prg
```

## Testing Strategy

### Unit Testing
- Test modules in isolation on Linux (mock serial)
- Validate protocol encoding/decoding
- Test editor operations
- Test word-wrap logic

### Integration Testing
- VICE emulator with virtual serial port
- Python test harness sends mock server responses
- Verify UI updates correctly
- Test scrolling, editing, all features

### Performance Testing
- Measure serial throughput
- Verify 60Hz main loop target
- Check keyboard responsiveness during streaming

## Known Limitations and Trade-offs

### Memory Constraints
- Limited scrollback (16 messages max in RAM)
- Long messages truncated/paginated
- Conversation history stored on Linux only

### Serial Performance
- 1200 baud = slow for long responses (~10 chars/sec displayed)
- User must be patient
- Status indicators critical for UX

### Character Set
- PETSCII vs ASCII conversion needed
- Some Unicode characters cannot be displayed
- May need custom charset for better symbols

### Screen Size
- 40 columns = frequent word wrapping
- 19 lines viewport = limited context
- Scrolling required for longer conversations

## Future Enhancements (Post-MVP)
- Syntax highlighting for code blocks (use custom charset)
- Disk save/load of conversations
- Configuration menu (baud rate, colors, etc.)
- Copy/paste from chat to editor
- Search in conversation history
- Multi-conversation tabs
- REU support for larger scrollback (C64/128)
