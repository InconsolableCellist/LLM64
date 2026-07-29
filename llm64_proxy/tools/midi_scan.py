#!/usr/bin/env python3
"""Stage 2 of the MIDI pipeline: read every file and describe it.

This replaces THREE stages of the SID pipeline at once, which is the
main reason MIDI is cheaper than SID:

  sid_scan + sid_reloc_batch  - gone. Those existed only to fit a 6502
                                player into the client's 4 KB window.
                                A .MID is handed to MCI as-is.
  sid_songlengths             - was an MD5 lookup into HVSC's database.
                                A MIDI's duration is arithmetic over its
                                own tempo map, exact and local.
  sid_loudness                - was three hours of SID emulation to
                                measure RMS. Velocity and CC7 are in the
                                file; no synthesis required.

What comes out feeds two consumers: the mood tagger (which wants the
words - track names, instrument names) and the library (which wants the
numbers - duration, and the quality signals below).

Two judgements worth naming, because they are the ones that decide
whether the soundtrack is pleasant rather than merely present:

MT-32 vs GM. A file voiced for a Roland MT-32 played on a General MIDI
device gets the wrong patch for every part - a lead becomes a
harpsichord. There is no flag for this, so we infer: a GM/GS/XG reset
SysEx means the sequencer was thinking in GM. Roland MT-32 SysEx
(manufacturer 0x41, model 0x16) means it was not.

Mechanical vs sequenced. A file dumped from a tracker or auto-converted
has near-constant velocity; one a human sequenced breathes. Velocity
spread is a cheap, honest proxy for "did somebody actually perform
this", and it stands in for the DeepSID ranking the SID library gets and
MIDI has no equivalent of.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

# --- SMF parsing -------------------------------------------------------
#
# Self-contained on purpose: mido would do this, but it is a dependency
# the proxy does not otherwise carry, and the format is small enough
# that owning the parser is cheaper than owning the dependency.


class BadMidi(Exception):
    pass


def _varlen(buf: bytes, i: int):
    """SMF variable-length quantity -> (value, next index)."""
    val = 0
    for _ in range(4):
        if i >= len(buf):
            raise BadMidi("truncated varlen")
        b = buf[i]
        i += 1
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            return val, i
    raise BadMidi("overlong varlen")


def _unwrap_rmid(data: bytes) -> bytes:
    """Some files are RIFF-wrapped (.RMI content saved as .mid)."""
    if data[:4] == b"RIFF" and data[8:12] == b"RMID":
        i = data.find(b"MThd")
        if i < 0:
            raise BadMidi("RMID without MThd")
        return data[i:]
    return data


def parse(data: bytes) -> dict:
    """Parse a standard MIDI file into the facts we care about."""
    data = _unwrap_rmid(data)
    if data[:4] != b"MThd":
        raise BadMidi("no MThd")
    fmt = int.from_bytes(data[8:10], "big")
    ntrks = int.from_bytes(data[10:12], "big")
    division = int.from_bytes(data[12:14], "big")
    if division & 0x8000:
        # SMPTE timing: frames/sec * ticks/frame. Rare, but real.
        frames = 256 - (division >> 8)
        ticks_per_sec = frames * (division & 0xFF)
        tpq = None
    else:
        tpq = division or 96
        ticks_per_sec = None

    texts = {"name": [], "instrument": [], "copyright": [], "text": [],
             "marker": []}
    tempos = []            # (abs_tick, usec_per_quarter)
    programs = set()       # GM program numbers actually selected
    channels = set()
    notes = 0
    drum_notes = 0
    velocities = []
    end_tick = 0
    sysex_gm = sysex_gs = sysex_xg = sysex_mt32 = False

    i = int.from_bytes(data[4:8], "big") + 8
    tracks_seen = 0
    while i < len(data) and tracks_seen < ntrks:
        if data[i:i + 4] != b"MTrk":
            # Junk between chunks happens in the wild; skip a byte and
            # resync rather than declaring the whole file dead.
            i += 1
            continue
        tlen = int.from_bytes(data[i + 4:i + 8], "big")
        start = i + 8
        end = min(start + tlen, len(data))
        tracks_seen += 1
        i = end

        j, tick, status = start, 0, 0
        while j < end:
            delta, j = _varlen(data, j)
            tick += delta
            if j >= end:
                break
            b = data[j]
            if b == 0xFF:                                   # meta
                j += 1
                mtype = data[j]
                j += 1
                mlen, j = _varlen(data, j)
                payload = data[j:j + mlen]
                j += mlen
                if mtype == 0x51 and mlen == 3:
                    tempos.append((tick,
                                   int.from_bytes(payload, "big") or 500000))
                elif mtype in (0x01, 0x02, 0x03, 0x04, 0x06):
                    s = payload.decode("latin-1", "replace").strip()
                    s = " ".join(s.split())
                    key = {0x01: "text", 0x02: "copyright", 0x03: "name",
                           0x04: "instrument", 0x06: "marker"}[mtype]
                    if s:
                        texts[key].append(s)
            elif b in (0xF0, 0xF7):                         # sysex
                j += 1
                slen, j = _varlen(data, j)
                payload = data[j:j + slen]
                j += slen
                if payload[:4] == b"\x7e\x7f\x09\x01":
                    sysex_gm = True
                elif payload[:1] == b"\x41":
                    # Roland. Model id is byte 2 (after device id):
                    # 0x42 = GS, 0x16 = MT-32/CM-32L.
                    if len(payload) > 2 and payload[2] == 0x42:
                        sysex_gs = True
                    elif len(payload) > 2 and payload[2] == 0x16:
                        sysex_mt32 = True
                elif payload[:1] == b"\x43":
                    sysex_xg = True
            else:
                if b & 0x80:
                    status = b
                    j += 1
                elif not status:
                    raise BadMidi("data byte with no running status")
                high, chan = status & 0xF0, status & 0x0F
                if high in (0xC0, 0xD0):
                    j += 1
                    if high == 0xC0:
                        # Channel 10 is percussion; its "program" is a
                        # drum kit, not a GM instrument, so it would
                        # pollute the instrument palette.
                        if chan != 9 and j - 1 < end:
                            programs.add(data[j - 1])
                else:
                    if j + 1 >= end:
                        break
                    note_vel = data[j + 1]
                    if high == 0x90 and note_vel > 0:       # real note-on
                        notes += 1
                        channels.add(chan)
                        velocities.append(note_vel)
                        if chan == 9:
                            drum_notes += 1
                    j += 2
        end_tick = max(end_tick, tick)

    if notes == 0:
        raise BadMidi("no notes")

    # --- duration: integrate the tempo map over the tick timeline ------
    if ticks_per_sec:
        seconds = end_tick / ticks_per_sec
    else:
        tempos.sort()
        if not tempos or tempos[0][0] > 0:
            tempos.insert(0, (0, 500000))       # SMF default = 120 bpm
        seconds, prev_tick, prev_us = 0.0, 0, tempos[0][1]
        for t_tick, us in tempos[1:]:
            t_tick = min(t_tick, end_tick)
            if t_tick > prev_tick:
                seconds += (t_tick - prev_tick) * prev_us / 1e6 / tpq
            prev_tick, prev_us = t_tick, us
        if end_tick > prev_tick:
            seconds += (end_tick - prev_tick) * prev_us / 1e6 / tpq

    vel_mean = statistics.fmean(velocities)
    vel_sd = statistics.pstdev(velocities) if len(velocities) > 1 else 0.0

    return {
        "format": fmt,
        "tracks": tracks_seen,
        "seconds": round(seconds, 1),
        "notes": notes,
        "channels": len(channels),
        "programs": sorted(programs),
        "drum_fraction": round(drum_notes / notes, 3),
        "notes_per_sec": round(notes / seconds, 2) if seconds > 0 else 0.0,
        "vel_mean": round(vel_mean, 1),
        "vel_sd": round(vel_sd, 1),
        "gm_reset": sysex_gm or sysex_gs or sysex_xg,
        "mt32": sysex_mt32 and not (sysex_gm or sysex_gs or sysex_xg),
        "names": texts["name"][:12],
        "instruments": texts["instrument"][:12],
        "copyright": texts["copyright"][:2],
        "text": texts["text"][:6],
    }


# --- what makes a tune usable -----------------------------------------
#
# A background score for a text adventure has requirements a music
# archive does not know about: it has to loop without being noticed, it
# must not be eight seconds of fanfare, and it must not be a solo
# harpsichord because it was written for an MT-32.

def verdict(f: dict) -> str:
    """'' when the tune is usable, else why it is not."""
    if f["seconds"] < 20:
        return "too short"
    if f["seconds"] > 900:
        return "too long"
    if f["notes"] < 60:
        return "too sparse"
    if f["mt32"]:
        return "MT-32 voiced"
    if f["drum_fraction"] > 0.85:
        return "drums only"
    if f["channels"] < 2 and f["notes_per_sec"] < 2:
        return "one thin part"
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    root_default = (Path(__file__).resolve().parents[1]
                    / "data" / "midi" / "vgmusic")
    ap.add_argument("--root", type=Path, default=root_default)
    ap.add_argument("--index", type=Path, default=None,
                    help="fetcher index.json (default: <root>/index.json)")
    ap.add_argument("--out", type=Path, default=None,
                    help="scan output (default: <root>/../scan.json)")
    args = ap.parse_args()

    index_path = args.index or (args.root / "index.json")
    out_path = args.out or (args.root.parent / "scan.json")
    entries = json.loads(index_path.read_text())

    kept, rejected, broken = [], {}, 0
    for n, e in enumerate(entries, 1):
        p = args.root / e["path"]
        try:
            feats = parse(p.read_bytes())
        except (BadMidi, OSError, IndexError, ValueError) as exc:
            broken += 1
            rejected.setdefault(f"unparseable ({type(exc).__name__})", 0)
            rejected[f"unparseable ({type(exc).__name__})"] += 1
            continue
        why = verdict(feats)
        if why:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        kept.append(dict(e, **feats))
        if n % 1000 == 0:
            print(f"  {n}/{len(entries)}  kept={len(kept)}", flush=True)

    out_path.write_text(json.dumps(kept, indent=1))
    total = len(entries)
    print(f"\nscanned {total}, kept {len(kept)} "
          f"({100 * len(kept) / max(total, 1):.0f}%)")
    for why, n in sorted(rejected.items(), key=lambda kv: -kv[1]):
        print(f"  rejected {n:6d}  {why}")
    if kept:
        secs = sorted(t["seconds"] for t in kept)
        print(f"\nduration median {secs[len(secs) // 2] / 60:.1f} min, "
              f"total {sum(secs) / 3600:.1f} h")
        gm = sum(1 for t in kept if t["gm_reset"])
        print(f"GM/GS/XG reset present in {100 * gm / len(kept):.0f}%")
    print(f"-> {out_path}")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
