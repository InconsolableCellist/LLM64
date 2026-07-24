#!/usr/bin/env python3
"""Stage 5 of the SID pipeline: measure per-tune loudness.

Runs each relocated .sid's init+play in a py65 6502 emulator, feeds the
SID register writes into pyresidfp (cycle-exact reSIDfp), renders a
stretch of audio, and reports RMS loudness plus how the tune treats the
master volume/filter register ($D418):

  d418_init  - value written during init (0xF default if never written)
  d418_live  - True if play() writes $D418 (a client-side override would
               fight it; only safe if the write is every frame anyway)

Needs the analysis venv: pyresidfp + py65 (pip install pyresidfp py65).
Run with -j matched to the UPS budget (default 4).
"""

import argparse
import json
import math
import struct
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

PAL_CLOCK = 985248.0
FRAME_CYCLES = int(PAL_CLOCK / 50)
SENTINEL = 0xFEFF
MAX_STEPS = 200_000     # per init call; play calls use a tighter budget
PLAY_STEPS = 20_000
SKIP_FRAMES = 50        # let envelopes settle before measuring
MEASURE_FRAMES = 750    # 15 seconds


def load_sid(path: Path):
    data = path.read_bytes()
    version, data_offset, load, init, play, songs, start_song = \
        struct.unpack(">HHHHHHH", data[4:0x12])
    payload = data[data_offset:]
    if load == 0:
        load = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    return load, init or load, play, max(0, start_song - 1), payload


def trace_d418(path_str):
    """Fast pass (no audio): run 300 frames, collect the distinct values
    the play routine writes to $D418. A tune that always writes the same
    value is safe to override for normalization; a varying one (volume
    tremolo, digis) must be left alone."""
    from py65.devices.mpu6502 import MPU
    from py65.memory import ObservableMemory

    path = Path(path_str)
    load, init, play, song, payload = load_sid(path)
    writes = []
    mem = ObservableMemory()
    mem.subscribe_to_write(range(0xD418, 0xD419), lambda addr, value:
                           writes.append(value & 0xFF))
    mpu = MPU(memory=mem)
    mem.write(load, payload)

    def call(addr, a, budget):
        mpu.pc = addr
        mpu.a = a
        mem.write(0x01FC, [(SENTINEL - 1) & 0xFF, (SENTINEL - 1) >> 8])
        mpu.sp = 0xFB
        for _ in range(budget):
            mpu.step()
            if mpu.pc == SENTINEL:
                return True
        return False

    if not call(init, song, MAX_STEPS):
        return {"file": path.name, "error": "init-hang"}
    writes.clear()  # init writes don't count as "live"
    for _ in range(300):
        if not call(play, 0, PLAY_STEPS):
            return {"file": path.name, "error": "play-hang"}
    return {"file": path.name, "d418_values": sorted(set(writes))}


def measure(path_str):
    from py65.devices.mpu6502 import MPU
    from py65.memory import ObservableMemory
    from pyresidfp import SoundInterfaceDevice
    from pyresidfp.registers import WritableRegister

    path = Path(path_str)
    load, init, play, song, payload = load_sid(path)

    writes = []
    mem = ObservableMemory()
    mem.subscribe_to_write(range(0xD400, 0xD419), lambda addr, value:
                           writes.append((mpu.processorCycles,
                                          addr - 0xD400, value & 0xFF)))
    mpu = MPU(memory=mem)
    mem.write(load, payload)

    def call(addr, a, budget):
        # emulate JSR addr with a sentinel return address
        mpu.pc = addr
        mpu.a = a
        mpu.sp = 0xFD
        mem.write(0x01FC, [(SENTINEL - 1) & 0xFF, (SENTINEL - 1) >> 8])
        mpu.sp = 0xFB
        for _ in range(budget):
            mpu.step()
            if mpu.pc == SENTINEL:
                return True
        return False

    if not call(init, song, MAX_STEPS):
        return {"file": path.name, "error": "init-hang"}

    d418_init = None
    for _, reg, val in writes:
        if reg == 0x18:
            d418_init = val

    sid = SoundInterfaceDevice(clock_frequency=PAL_CLOCK)
    for _, reg, val in writes:
        sid.write_register(WritableRegister(reg), val)
    writes.clear()

    sumsq = 0.0
    nsamples = 0
    d418_live_frames = 0
    for frame in range(SKIP_FRAMES + MEASURE_FRAMES):
        base = mpu.processorCycles
        if not call(play, 0, PLAY_STEPS):
            return {"file": path.name, "error": "play-hang"}
        # replay this frame's register writes at their cycle offsets
        prev = 0
        frame_d418 = False
        for cyc, reg, val in writes:
            off = min(cyc - base, FRAME_CYCLES - 1)
            if off > prev:
                sid.clock(timedelta(seconds=(off - prev) / PAL_CLOCK))
                prev = off
            sid.write_register(WritableRegister(reg), val)
            if reg == 0x18:
                frame_d418 = True
        writes.clear()
        samples = sid.clock(
            timedelta(seconds=(FRAME_CYCLES - prev) / PAL_CLOCK))
        if frame_d418:
            d418_live_frames += 1
        if frame >= SKIP_FRAMES:
            for s in samples:
                sumsq += s * s
            nsamples += len(samples)

    rms = math.sqrt(sumsq / max(1, nsamples))
    return {
        "file": path.name,
        "rms_db": round(20 * math.log10(max(rms, 1e-6)), 1),
        "d418_init": 0x0F if d418_init is None else d418_init,
        "d418_live": d418_live_frames > MEASURE_FRAMES // 2,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("siddir", type=Path)
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--d418-only", action="store_true",
                    help="fast pass: trace $D418 write values, no audio")
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    files = sorted(str(p) for p in args.siddir.glob("*.sid"))
    worker = trace_d418 if args.d418_only else measure
    results = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for i, res in enumerate(pool.map(worker, files)):
            results.append(res)
            if (i + 1) % 50 == 0:
                print(f"{i + 1}/{len(files)}", file=sys.stderr, flush=True)
            args.output.write_text(json.dumps(results, indent=1))

    ok = [r for r in results if "rms_db" in r]
    if ok:
        vals = sorted(r["rms_db"] for r in ok)
        print(f"{len(ok)}/{len(results)} measured; rms_db min={vals[0]} "
              f"median={vals[len(vals)//2]} max={vals[-1]}")


if __name__ == "__main__":
    main()
