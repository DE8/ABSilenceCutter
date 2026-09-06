# ABSilenceCutter

**ABSilenceCutter** is a Python CLI for automatically reducing pauses in M4B audiobooks with embedded chapters. It analyzes each chapter with FFmpeg, compresses only the regions detected as silence, rebuilds the audiobook, and updates chapter timestamps.

The original file is never modified. The output file is published only after a final verification of duration, chapters, audio, and cover art.

## Features

- Silence detection using FFmpeg's `silencedetect` filter
- Configurable compression of pauses only, without speeding up speech
- Parallel chapter processing with `--workers`
- Chapter reconstruction with updated timestamps
- Preservation of global metadata, chapter titles, and embedded cover art
- Persistent working directory for resuming interrupted processing
- Automatic output verification before replacing the final file
- No external Python dependencies

## Requirements

| Component | Version / requirement |
|---|---|
| Python | 3.9 or later |
| FFmpeg | `ffmpeg` and `ffprobe` must be available in `PATH` |
| Input | An `.m4b` file with at least one embedded chapter |

Check that the tools are available:

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

### Installing FFmpeg

| System | Command |
|---|---|
| macOS with Homebrew | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt update && sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch Linux | `sudo pacman -S ffmpeg` |
| Windows with winget | `winget install Gyan.FFmpeg` |

After installation, close and reopen the terminal if `ffmpeg -version` cannot be found.

## Installation

### Recommended method: pipx

```bash
pipx install "git+https://github.com/DE8/ABSilenceCutter.git"
```

Verify the installation:

```bash
ab-silence-cutter --help
```

### Alternative: pip

Install into the active Python environment:

```bash
python3 -m pip install "git+https://github.com/DE8/ABSilenceCutter.git"
```

## Updating

With `pipx`:

```bash
pipx upgrade ab-silence-cutter
```

With `pip`:

```bash
python3 -m pip install --upgrade "git+https://github.com/DE8/ABSilenceCutter.git"
```

## Quick usage

```bash
ab-silence-cutter INPUT.m4b OUTPUT.m4b
```

## Examples

### Balanced configuration

```bash
ab-silence-cutter input.m4b output.m4b \
  --workers 4 \
  --silence-threshold -40 \
  --min-silence 0.5 \
  --compression-ratio 0.5
```

This halves pauses of at least 0.5 seconds detected below -40 dB.

### More aggressive compression

```bash
ab-silence-cutter input.m4b output.m4b \
  --workers 24 \
  --silence-threshold -40 \
  --min-silence 0.35 \
  --compression-ratio 0.3 \
  --max-output-silence 1
```

This retains 30% of each selected pause and never leaves more than one second of remaining silence per individual pause.

### Resuming after an interruption

If you interrupt the process with `Ctrl+C`, the working directory remains on disk. Run the same command again to reuse already completed chapters:

```bash
ab-silence-cutter input.m4b output.m4b \
  --workers 8 \
  --silence-threshold -40 \
  --min-silence 0.5 \
  --compression-ratio 0.5
```

Do not change the input, output, working-directory path, or processing parameters if you want to maximize reuse of intermediate files. Changing only `--workers` does not invalidate already processed chapters.

### Keeping the working directory

```bash
ab-silence-cutter input.m4b output.m4b --keep-work-dir
```

To choose an explicit path:

```bash
ab-silence-cutter input.m4b output.m4b \
  --work-dir "$HOME/M4B-work/book-name" \
  --keep-work-dir \
  --workers 6
```

> [!CAUTION]
> `--work-dir` must not contain either the input or the output. Intermediate FLAC files can require substantial disk space.

## CLI options

| Option | Default | Description |
|---|---:|---|
| `INPUT` | — | Source M4B file; it is never modified |
| `OUTPUT` | — | Path for the new M4B file |
| `--workers N` | `1` to `4` | Maximum number of chapters processed in parallel. In general, one worker per logical CPU core is recommended. |
| `--work-dir PATH` | `OUTPUT.chapters` | Directory for intermediate files and resume state |
| `--keep-work-dir` | disabled | Keeps the working directory even after a successful run |
| `--silence-threshold DB` | `-40` | Silence-detection threshold in dB |
| `--min-silence SECONDS` | `0.5` | Minimum duration of a pause to compress |
| `--compression-ratio RATIO` | `0.5` | Fraction of the silence duration to retain; must be between `0` and `1` |
| `--max-output-silence SECONDS` | disabled | Maximum remaining duration of each selected pause |
| `--audio-codec CODEC` | `aac` | Encoder used to re-encode audio, for example `aac` |
| `--audio-bitrate BITRATE` | input bitrate or fallback | Output bitrate, for example `64k`, `96k`, or `128k` |
| `--overwrite` | disabled | Allows an existing output file to be replaced |
| `--verbose` | disabled | Prints a summary for every processed chapter |

See all options directly from the terminal:

```bash
ab-silence-cutter --help
```

## How it works

1. `ffprobe` reads the M4B duration, chapters, metadata, audio streams, and cover art.
2. The tool identifies chapter intervals and extracts their audio into temporary FLAC files.
3. FFmpeg detects pauses according to the configured threshold and minimum duration.
4. Speech remains unchanged; each selected pause is shortened by speeding up only that segment.
5. Chapters are encoded, concatenated, and inserted into an M4B container.
6. Embedded chapters are regenerated using the actual durations of the produced audio.
7. The result is verified before being moved to the output path.

## Output and resuming

The default working directory is `OUTPUT.chapters/` and contains one subdirectory per chapter:

```text
output.chapters/
├── 0001/
│   ├── source.flac
│   ├── processed.m4a
│   ├── audio_filter.txt
│   └── state.json
├── 0002/
│   └── ...
├── chapters.ffconcat
└── chapters.ffmeta
```

Each `state.json` stores the input and parameter fingerprint, processing state, measured durations, and the information needed to resume the work correctly. If execution is interrupted or fails, valid chapters remain available for a subsequent run with the same parameters.

## What is preserved

The tool attempts to preserve:

- Global audiobook metadata
- Embedded cover art, when identified as `attached_pic`
- Chapter title and other chapter tags
- Source audio sample rate and channel count
- Chapter structure, with recalculated timestamps

Streams other than the main audio stream and cover art are not retained. A warning is shown during processing in that case.

## Troubleshooting

### `ffmpeg` or `ffprobe` not found

Install FFmpeg and ensure both commands are in `PATH`:

```bash
ffmpeg -version
ffprobe -version
```

### `ab-silence-cutter: command not found`

Check that the installation completed successfully:

```bash
python3 -m pip show absilencecutter
```

If you used `pip install --user`, add the user scripts directory to `PATH`, or reinstall with `pipx`.

### The output already exists

The tool does not replace an existing file without explicit permission:

```bash
ab-silence-cutter input.m4b output.m4b --overwrite
```

### Processing was interrupted or failed

Do not delete the `OUTPUT.chapters/` directory. Run the same command again: completed chapters with compatible parameters will be reused.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Processing completed |
| `2` | Invalid arguments or paths |
| `3` | `ffmpeg` or `ffprobe` unavailable |
| `4` | Processing error |
| `5` | Final verification failed |
| `130` | Process interrupted; completed states remain available |