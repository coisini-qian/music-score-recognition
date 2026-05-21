# Music Score Recognition Hub

This workspace contains three separate projects:

- `audiveris/` for score recognition
- `PianoBooster/` for practice and correction
- `VMPK/` for virtual keyboard demos

The new integration layer lives in `orchestrator/`.

## What it does

- Capture a score photo from the browser camera or upload an image
- Send the image to Audiveris
- Collect the exported MusicXML output
- Optionally convert MusicXML to MIDI
- Launch PianoBooster with the generated MIDI
- Launch VMPK for input/demo use

## Quick start

1. Copy `orchestrator/config.example.json` to `orchestrator/config.json`.
2. Fill in the Audiveris, converter, PianoBooster, and VMPK command templates.
3. Run `python orchestrator/app.py`.
4. Open `http://127.0.0.1:8765` in a browser.

## Important note

Audiveris exports MusicXML by design. MIDI generation is handled here as an optional
conversion step, so the converter command must be configured if you want PianoBooster to
open a MIDI file automatically.
本项目已在 Windows 11 环境下完成本地部署与功能测试。