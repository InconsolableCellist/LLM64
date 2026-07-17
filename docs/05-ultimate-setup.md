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
   - Interrupt: **NMI or IRQ both work** — the client installs handlers
     on both vectors and dispatches on the ACIA status register.
   - Mode: SwiftLink. Note SwiftLink's clock doubles the baud table:
     the client's `$1E` control value (9600 on a stock 6551) runs at
     19200. The Ultimate's modem side follows automatically; nothing to
     change on the proxy.
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
- **Garbled screen during replies**: baud mismatch. Try the Turbo232
  setting or a firmware where the modem tracks the SwiftLink rate
  doubling (see note above).
- The scripted diagnostic build shows byte-level detail on screen:
  `make -C c64_client CONNECT=hayes SERVER_IP=<ip> DEBUG_CLIENT=1`
