# movie-ascii

Play **videos**, **GIFs**, and **images** as ASCII art directly in your terminal — with colour, audio, subtitles, and export support.

![demo](https://github.com/marabunta-labs/movie-ascii/blob/main/assets/demo.gif?raw=true)

## Features

- **Three render modes** — `bw` (black & white), `ascii-color` (16 ANSI colours), `truecolor` (full RGB)
- **Audio sync** — plays original audio alongside the ASCII frames via ffpyplayer
- **Subtitles** — auto-fetches YouTube subtitles and overlays them during playback
- **GIF support** — plays animated GIFs frame-by-frame at their original speed
- **Export** — saves the ASCII render to `.png`, `.jpg`, `.gif`, `.mp4` (with audio), or `.html`
- **YouTube streaming** — pass any YouTube URL directly, no download needed
- **Custom charsets** — use a built-in alphabet or define your own inline
- **Wizard** — get a resolution recommendation based on your terminal size
- **Playback controls** — pause, scrub ±5 s, quit

## Requirements

- Python ≥ 3.9
- A terminal with UTF-8 support and truecolor (iTerm2, Alacritty, kitty, Windows Terminal, etc.)

## Installation

```bash
pip install movie-ascii
```

## Quick start

```bash
# Play a local video in black & white
movie-ascii clip.mp4

# Truecolor at 120 columns
movie-ascii clip.mp4 -m truecolor -w 120

# Stream from YouTube with subtitles in English
movie-ascii https://youtu.be/dQw4w9WgXcQ -m ascii-color -l en

# Play an animated GIF
movie-ascii animation.gif -m truecolor

# Export to MP4 (with audio)
movie-ascii clip.mp4 -m truecolor --save output.mp4

# Export first frame to PNG
movie-ascii photo.jpg --save photo_ascii.png

# Export animated GIF
movie-ascii animation.gif -m ascii-color --save animation_ascii.gif
```

## Playback controls

| Key | Action |
|-----|--------|
| `Space` / `K` | Pause / resume |
| `J` | Seek −5 seconds |
| `L` | Seek +5 seconds |
| `Q` | Quit |

## Options

```
usage: movie-ascii [-h] [-w COLS] [-m MODE] [-c NAME|CHARS] [--colors BITS]
                   [-l LANG] [--save FILE] [--grid] [-L] [--wizard] [FILE|URL]

positional arguments:
  FILE|URL              Path or URL to a video, GIF, or image.

options:
  -h, --help            Show this help message and exit.
  -w, --width COLS      Output width in characters (default: 80% of terminal width).
  -m, --mode MODE       bw | ascii-color | truecolor  (default: bw)
  -c, --charset NAME|CHARS
                        Built-in charset name or a custom string (2–256 chars).
  --colors BITS         Truecolor quantization depth: 3–8 bits/channel (default 5).
                        Lower = less data, higher = more detail.
  -l, --lang LANG       Subtitle language for YouTube videos (default: es).
  --save FILE           Export to .png / .jpg / .gif / .mp4 / .html instead of playing.
  --grid                Open an HTML grid comparing all render modes in a browser.
  -L, --list-charsets   List all built-in charsets with a preview and exit.
  --wizard              Show a resolution guide based on your terminal size and exit.
```

## Charsets

```bash
movie-ascii --list-charsets       # show all built-in options
movie-ascii clip.mp4 -c blocks    # use the built-in block charset
movie-ascii clip.mp4 -c " .:▒█"  # use a custom inline string
```

Built-in charsets: `standard`, `simple`, `numeric`, `alphabet`, `punctuation`, `braille`, `block`, `blocks`

## Resolution wizard

Not sure which width to use? Run:

```bash
movie-ascii --wizard
```

It reads your terminal dimensions and shows a table of width options with estimated MB/s per render mode, colour-coded safe / ok / slow.

## Performance notes

- **bw** is the lightest mode — safe at any width.
- **ascii-color** uses run-length encoding — usually safe up to full terminal width.
- **truecolor** generates the most data. Use `--colors 3` or `--colors 4` to reduce it, or follow the wizard's recommendation for a safe width.

## Export notes

- `.mp4` export includes the original audio track (bundled ffmpeg via `imageio-ffmpeg` — no system install needed).
- `.html` export creates a self-contained file with JS-animated frames, no server required.
- `.gif` export uses Pillow; large/long videos may produce big files.

## License

MIT
