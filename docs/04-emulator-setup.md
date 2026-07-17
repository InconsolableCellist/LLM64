# Emulator Setup (VICE)

How to run the client in VICE, and why earlier attempts failed. Everything
here is automated by `emu/` — this doc explains what those scripts do.

## The short version

```bash
make test-emu        # scripted protocol session, direct pipe, warp speed
make test-emu-long   # ~3KB streamed response, asserts zero CRC failures
make test-emu-hayes  # full AT-command dial via tcpser (mirrors real hardware)
make test-emu-tui    # interactive TUI driven by injected keystrokes
make test-all        # all of the above
make run-live        # interactive TUI against the real API (config.toml)
```

Each test boots a mock OpenAI SSE server (or the real one), the Python
proxy, and `x64sc`, then reads the emulated screen RAM through VICE's
binary monitor to assert progress. Screenshots land in `emu/artifacts/`.

## Why this never worked before (troubleshooting archaeology)

Four separate problems stacked on top of each other:

1. **Wrong VICE device.** The client drives a 6551 ACIA at `$DE00`
   (SwiftLink-compatible). VICE's *userport RS232* settings configure a
   completely different device at a different address — no combination of
   its options can ever work. The ACIA cartridge emulation is enabled with
   `-acia1`, and `-myaciadev 0` routes it to RS232 device 1 (`-rsdev1`).
   Without `-myaciadev 0` the ACIA defaults to device index 1, which is
   `/dev/ttyS1` — bytes silently go to a real serial port.

2. **No modem behind the chip.** VICE emulates the ACIA *chip*, not a
   Hayes modem. `ATZ`/`ATDT` go down a raw byte pipe and nothing answers
   `OK`/`CONNECT`. The C64 Ultimate emulates the modem itself, which is
   why the client speaks AT commands at all. In VICE you either run
   **tcpser** as the modem (`-rsdev1ip232`) or skip AT entirely with the
   `CONNECT=direct` client build and point `-rsdev1` at the proxy.

3. **PETSCII on the wire.** cc65 translates C string literals to PETSCII,
   so `"ATZ"` was transmitted as `$C1 $D4 $DA` — no modem recognizes that
   as an AT command (uppercase letters land in the high PETSCII range).
   Fixed by converting at the serial boundary (`src/text.c`).

4. **Warp vs. wall clock.** `-warp` runs the C64's cycle-based timeouts
   10-20x faster than real time, while tcpser answers on a wall-clock
   schedule — the client gave up before the modem replied. Hayes-mode
   tests run unwarped; direct mode (no handshake) can warp.

## The working VICE flags

```
x64sc -acia1 -acia1mode 0 -acia1base 0xDE00 -acia1irq 2 -myaciadev 0 \
      -rsdev1 127.0.0.1:6400 +rsdev1ip232 -rsdev1baud 9600 \
      -autostartprgmode 1 -autostart c64_client/build/c64llm.prg
```

- `-acia1mode 0` = plain 6551 (control register `$1E` -> 9600 baud).
  Mode 1 (SwiftLink) doubles the rates: the same register value means
  19200. Keep the client and VICE in agreement.
- `-rsdev1 host:port` makes VICE open a raw TCP connection; with
  `-rsdev1ip232` it speaks tcpser's IP232 framing instead.
- `-autostartprgmode 1` injects the PRG into RAM — the tape-emulation
  autostart path is flaky when the binary monitor pauses the machine.

For the Hayes flow, start tcpser first:

```
tcpser -v 25232 -s 9600 -p 25233 -tSs
```

(`-p` matters: tcpser's *inbound call* listener defaults to port 6400,
which collides with the proxy.) Then use `-rsdev1 127.0.0.1:25232
-rsdev1ip232` and build the client with
`make -C c64_client CONNECT=hayes SERVER_IP=127.0.0.1`.

## Test harness internals

- `emu/vice_monitor.py` — minimal VICE binary-monitor client: reads
  screen RAM at `$0400` (decoded from screen codes), injects keystrokes,
  quits the emulator (which triggers `-exitscreenshot`).
- `emu/mock_llm.py` — stdlib OpenAI-compatible SSE server with canned
  responses (`LONGTEST` in the prompt selects a ~3KB reply).
- `emu/test_e2e.py` — orchestrates mock -> proxy -> (tcpser) -> VICE and
  polls the screen for expected text. Exit code 0 = pass.

Debugging a failure: `emu/artifacts/` has per-process logs
(`proxy.log`, `vice.log`, `tcpser.log`), `fail-*.txt` screen dumps at the
moment of timeout, and the final PNG screenshot.
