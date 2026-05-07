# Orchestrator

This is a small local web app that glues together Audiveris, PianoBooster, and VMPK.

## Setup

1. Copy `config.example.json` to `config.json`.
2. Update the command templates for your local builds or installed binaries.
3. Run:

```bash
python app.py
```

## Command templates

The following placeholders are supported:

- `{root}`: workspace root
- `{workdir}`: per-job working directory
- `{input}`: uploaded score image path
- `{output}`: Audiveris output directory
- `{musicxml}`: exported MusicXML file
- `{midi}`: generated MIDI file

## Pipeline

1. Capture or upload an image.
2. Run Audiveris in batch/export mode.
3. Find the generated MusicXML file.
4. Optionally convert MusicXML to MIDI.
5. Open PianoBooster with the MIDI.
6. Open VMPK for testing/demo input.
