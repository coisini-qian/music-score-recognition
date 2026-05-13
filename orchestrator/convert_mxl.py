"""MusicXML to MIDI converter using only Python standard library."""
import os
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def midi_note(step: str, octave: int, alter: int = 0) -> int:
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[step.upper()]
    return (octave + 1) * 12 + base + alter


def var_len_bytes(value: int) -> bytes:
    buf = bytearray()
    buf.append(value & 0x7F)
    value >>= 7
    while value:
        buf.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(buf))


def write_midi_chunk(f, chunk_id: bytes, data: bytes) -> None:
    f.write(chunk_id)
    f.write(struct.pack(">I", len(data)))
    f.write(data)


def parse_musicxml(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[: root.tag.index("}") + 1]

    def tag(name: str) -> str:
        return f"{ns}{name}"

    parts = []
    for part_elem in root.iter(tag("part")):
        notes = []
        divisions = 1
        current_tick = 0
        tempo = 500000  # 120 BPM in microseconds per quarter note
        ticks_per_beat = 480

        for measure in part_elem.iter(tag("measure")):
            for attr in measure.iter(tag("attributes")):
                div_elem = attr.find(tag("divisions"))
                if div_elem is not None and div_elem.text:
                    divisions = int(div_elem.text)

            for direction in measure.iter(tag("direction")):
                sound = direction.find(tag("sound"))
                if sound is not None:
                    t = sound.get("tempo")
                    if t:
                        tempo = int(float(t))

            for note_elem in measure.iter(tag("note")):
                dur_elem = note_elem.find(tag("duration"))
                if dur_elem is None or not dur_elem.text:
                    continue
                duration_ticks = int(dur_elem.text)

                rest = note_elem.find(tag("rest"))
                if rest is not None:
                    current_tick += duration_ticks
                    continue

                pitch = note_elem.find(tag("pitch"))
                if pitch is None:
                    current_tick += duration_ticks
                    continue

                step_elem = pitch.find(tag("step"))
                octave_elem = pitch.find(tag("octave"))
                alter_elem = pitch.find(tag("alter"))
                if step_elem is None or octave_elem is None:
                    current_tick += duration_ticks
                    continue

                step = step_elem.text
                octave = int(octave_elem.text)
                alter = int(alter_elem.text) if alter_elem is not None and alter_elem.text else 0

                note_num = midi_note(step, octave, alter)
                note_num = max(0, min(127, note_num))

                velocity = 80
                dynamics = note_elem.find(tag("dynamics"))
                if dynamics is not None:
                    velocity = 90

                notes.append({
                    "start_tick": current_tick,
                    "duration_ticks": duration_ticks,
                    "note": note_num,
                    "velocity": velocity,
                })
                current_tick += duration_ticks

        parts.append({"notes": notes, "divisions": divisions, "tempo": tempo})

    return parts


def build_midi(parts: list[dict]) -> bytes:
    import io
    output = io.BytesIO()

    ticks_per_beat = 480

    # Header chunk
    header_data = struct.pack(">HHH", 1, len(parts), ticks_per_beat)
    write_midi_chunk(output, b"MThd", header_data)

    # Tempo track
    tempo_track = bytearray()
    tempo_track.extend(var_len_bytes(0))

    if parts:
        tempo_us = parts[0].get("tempo", 500000)
    else:
        tempo_us = 500000
    tempo_track.extend(b"\xFF\x51\x03")
    tempo_track.extend(struct.pack(">I", tempo_us)[1:])

    # End of tempo track
    tempo_track.extend(var_len_bytes(0))
    tempo_track.extend(b"\xFF\x2F\x00")
    write_midi_chunk(output, b"MTrk", bytes(tempo_track))

    for part in parts:
        track_data = bytearray()
        abs_ticks = 0
        note_events = []
        divisions = part.get("divisions", 1)
        scale = ticks_per_beat / divisions if divisions else 1

        for note in part["notes"]:
            start = int(note["start_tick"] * scale)
            end = start + int(note["duration_ticks"] * scale)
            note_events.append((start, 0x90, note["note"], note["velocity"]))
            note_events.append((end, 0x80, note["note"], 0))

        note_events.sort(key=lambda x: (x[0], x[1] == 0x90))

        for tick, event_type, note_num, vel in note_events:
            delta = tick - abs_ticks
            abs_ticks = tick
            track_data.extend(var_len_bytes(delta))
            track_data.extend(struct.pack("BBB", event_type, note_num, vel))

        # End of part track
        track_data.extend(var_len_bytes(0))
        track_data.extend(b"\xFF\x2F\x00")

        write_midi_chunk(output, b"MTrk", bytes(track_data))

    return output.getvalue()


def convert(input_path: str, output_path: str) -> str:
    input_path = os.path.abspath(input_path)
    output_path = os.path.abspath(output_path)
    xml_text: str

    if input_path.lower().endswith(".mxl"):
        with zipfile.ZipFile(input_path, "r") as zf:
            rootfile = None
            for name in zf.namelist():
                if name.lower().endswith(".xml") or name == "META-INF/container.xml":
                    pass
            # Read container.xml to find root file
            if "META-INF/container.xml" in zf.namelist():
                container = ET.fromstring(zf.read("META-INF/container.xml").decode("utf-8"))
                ns = ""
                for elem in container.iter():
                    if elem.tag.startswith("{"):
                        ns = elem.tag[: elem.tag.index("}") + 1]
                        break
                rootfiles = container.findall(f".//{ns}rootfile")
                if rootfiles:
                    rootfile = rootfiles[0].get("full-path")
            if rootfile and rootfile in zf.namelist():
                xml_text = zf.read(rootfile).decode("utf-8")
            else:
                # Find first XML file
                for name in zf.namelist():
                    if name.lower().endswith(".xml"):
                        xml_text = zf.read(name).decode("utf-8")
                        break
                else:
                    raise ValueError("No XML file found in MXL archive")
    else:
        xml_text = Path(input_path).read_text(encoding="utf-8")

    parts = parse_musicxml(xml_text)
    midi_data = build_midi(parts)
    Path(output_path).write_bytes(midi_data)
    return output_path


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.mxl|.xml> <output.mid>")
        sys.exit(1)
    result = convert(sys.argv[1], sys.argv[2])
    print(f"Converted: {sys.argv[1]} -> {result}")
