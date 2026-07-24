# Real Hardware Setup (C64 Ultimate / Ultimate 64)

Checklist for running the client on real hardware with the Ultimate's WiFi
modem emulation. The exact menu names vary by firmware version; the
concepts don't.

## What the client expects

- A 6551 ACIA (SwiftLink-compatible) at **$DE00**
- A Hayes modem behind it that answers `ATZ`/`ATE0`/`ATV1` and connects
  with `ATDT<host>:<port>`
- 8N1 framing; the client programs control register `$1E`

This is exactly what the Ultimate's "ACIA / SwiftLink" emulation with the
built-in modem provides over WiFi.

## Ultimate configuration checklist

1. **WiFi**: configure and verify the Ultimate is on your LAN
   (F2 menu -> Network / WiFi settings; give it connectivity to the
   machine running the proxy).
2. **ACIA emulation**: in the Cartridge/IO settings, enable the
   **ACIA (SwiftLink/Turbo232)** emulation:
   - Base address: **$DE00**
   - Interrupt: **NMI recommended.** A real SwiftLink cartridge raises
     NMI, and the client's NMI handler drains the ACIA even inside the
     interrupts-off windows that a disk load or a SID play routine
     create — which is what keeps the higher rates reliable. IRQ also
     works (both vectors are installed) and is what the VICE emulator
     uses; on hardware prefer NMI.
   - Mode: SwiftLink. Its crystal **doubles** the 6551 baud table, so
     the rate you pick on the C64 is the REAL hardware rate — the
     doubling is already baked into the labels, nothing to compute or
     halve. Pick it in the config editor (first boot, or **F1 → E →
     Speed**): **9600 / 19200 / 38400**, all honored on hardware. The
     Ultimate's modem side follows the ACIA rate automatically (nothing
     to set there), and the proxy auto-tunes its bulk pacing to whatever
     the client reports on connect — so there is nothing baud-related to
     change on the proxy either. (On the VICE emulator there is no
     doubled crystal, so the same setting runs at *half* the label; the
     test harness accounts for that.)
3. **Modem emulation**: enable the modem on the ACIA (this is what
   answers the AT commands). Command echo on/off doesn't matter — the
   client sends `ATE0` and also skips echoed characters.

## Build and deploy the client

```bash
# Bake in your proxy's LAN address:
make -C c64_client clean
make -C c64_client CONNECT=hayes SERVER_IP=192.168.1.39 SERVER_PORT=6400

# Copy build/c64llm.prg to the Ultimate however you like; with the
# Ultimate's FTP server enabled there's a shortcut:
make -C c64_client upload C64_IP=<ultimate-ip>
```

Start the proxy on the Linux box (reachable from the C64's network):

```bash
cd c64llm_proxy
.venv/bin/python -m src.main --host 0.0.0.0 --port 6400
```

Then on the C64: load and run `C64LLM.PRG`. You should see the dial
sequence in the status bar, then "Ready. Type your message."

## If it doesn't connect

- **No modem response**: ACIA emulation not enabled, wrong base address,
  or the modem emulation is off. The `make test-emu-hayes` suite runs
  the *identical* AT flow under tcpser, so client-side logic is already
  verified — hardware-side config is the variable.
- **CONNECT but no server reply**: proxy not reachable — check the IP
  baked into the PRG, the proxy is bound to `0.0.0.0`, and any firewall.
- **Garbled screen or dropped data during replies**: the rate is too
  high for this cartridge/firmware combination. Dial it down in **F1 → E
  → Speed** (38400 → 19200 → 9600), save, and reboot. Confirm the ACIA
  is set to **NMI** — on IRQ the higher rates lose bytes during disk
  loads and SID playback. The status bar's `hw` / `ov` / `cr` counters
  tell you where the loss is (`hw`/`ov` = the C64 side can't keep up,
  `cr` = corruption on the wire).
- The scripted diagnostic build shows byte-level detail on screen:
  `make -C c64_client CONNECT=hayes SERVER_IP=<ip> DEBUG_CLIENT=1`
