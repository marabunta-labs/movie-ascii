import argparse
import os
import sys
import base64
import webbrowser
import time
import subprocess
import tempfile
import shutil
from urllib.parse import urlparse, parse_qs
import select
import tty
import termios
import threading
import queue as _queue

if os.name != "nt":
    # Save the original physical error pipe
    original_stderr_fd = os.dup(sys.stderr.fileno())
    # Open a black hole (/dev/null)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    # Redirect the physical pipe to the black hole
    os.dup2(devnull_fd, sys.stderr.fileno())

import cv2
import numpy as np
import yt_dlp
try:
    from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageFont as _PILFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    _PILImage = _PILDraw = _PILFont = None

try:
    import imageio_ffmpeg as _iio_ffmpeg
    _FFMPEG_EXE = _iio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG_EXE = "ffmpeg"  # fall back to system ffmpeg if package unavailable
from youtube_transcript_api import YouTubeTranscriptApi
from ffpyplayer.player import MediaPlayer
from ffpyplayer.tools import set_loglevel

# Restore the pipe to see real errors from our code
if os.name != "nt":
    os.dup2(original_stderr_fd, sys.stderr.fileno())
    os.close(devnull_fd)
    os.close(original_stderr_fd)

# Alphabets
CHARSETS = {
    "standard": " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "simple": " .:-=+*#%@",
    "numeric": " 1723546908",
    "alphabet": " iltzcvxnoewakqpdmhgbfyZVXUYTJCLQOMWB",
    "punctuation": " .',:;!|?><+_-",
    "braille": " ⠁⠃⠇⠏⠟⠿⣿",
    "block": "█",
    "blocks": " ░▒▓█"
}

# HTML-escape translation table — used by pixels_to_html (module-level to avoid
# rebuilding the table on every call)
_HTML_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", " ": "&nbsp;"})

# ANSI 3-bit colour palette (ascii-color mode) shared by both the terminal
# renderer and the PIL-based exporter — defined once at module level.
_ANSI_PAL = np.array([
    (85, 85, 85), (255, 85, 85), (85, 255, 85), (255, 255, 85),
    (85, 85, 255), (255, 85, 255), (85, 255, 255), (255, 255, 255),
], dtype=np.uint8)


# ---------------------------------------------------------------------------
# PIL-based rendering helpers (used for --save export)
# ---------------------------------------------------------------------------

def _init_ascii_renderer(charset_name):
    """Build (glyph_atlas, char_w, char_h) bitmap atlas for PIL-based export."""
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow is required for export. Run: pip install pillow")
    chars = CHARSETS[charset_name]
    try:
        font = _PILFont.load_default(size=12)
    except TypeError:
        font = _PILFont.load_default()
    try:
        bb = font.getbbox("M")
        cw = max(bb[2] - bb[0], 1) + 2
        ch_font = max(bb[3] - bb[1], 1) + 3
    except Exception:
        cw, ch_font = 8, 14
    # Force ch = 2*cw so the aspect ratio of the exported image matches the
    # terminal view. resize_image already halves the row count (the *0.5 factor)
    # to compensate for terminal chars being ~2x taller than wide. The glyph
    # cell must honour the same 2:1 ratio or the output will be squished.
    ch = 2 * cw
    atlas = np.zeros((len(chars), ch, cw), dtype=bool)
    for ci, char in enumerate(chars):
        # Render glyph at its natural font height, then center it in the taller cell
        tmp = _PILImage.new("L", (cw, ch_font), 0)
        d = _PILDraw.Draw(tmp)
        try:
            bb = font.getbbox(char)
            ox = max((cw - (bb[2] - bb[0])) // 2, 0) - bb[0]
            oy = max((ch_font - (bb[3] - bb[1])) // 2, 0) - bb[1]
        except Exception:
            ox, oy = 0, 0
        d.text((ox, oy), char, fill=255, font=font)
        glyph = np.array(tmp) > 64
        y_off = (ch - ch_font) // 2
        y_end = min(y_off + ch_font, ch)
        atlas[ci, y_off:y_end, :] = glyph[:y_end - y_off, :]
    return atlas, cw, ch


def _render_frame_to_pil(resized_bgr, mode, charset_name, glyph_atlas, cw, ch, color_bits=5):
    """Vectorized BGR frame → PIL Image for file export."""
    quant_mask = 0xFF & ~((1 << (8 - color_bits)) - 1)
    image_rgb = cv2.cvtColor(resized_bgr, cv2.COLOR_BGR2RGB)
    chars = CHARSETS[charset_name]
    H, W = image_rgb.shape[:2]
    lum = (0.299 * image_rgb[:, :, 0].astype(np.float32)
           + 0.587 * image_rgb[:, :, 1].astype(np.float32)
           + 0.114 * image_rgb[:, :, 2].astype(np.float32))
    idx = np.clip((lum * ((len(chars) - 1) / 255.0)).astype(np.int32), 0, len(chars) - 1)
    if mode == "bw":
        colors = np.full((H, W, 3), 220, dtype=np.uint8)
    elif mode == "ascii-color":
        ai = ((image_rgb[:, :, 0] > 127).astype(np.int32)
              + (image_rgb[:, :, 1] > 127).astype(np.int32) * 2
              + (image_rgb[:, :, 2] > 127).astype(np.int32) * 4)
        colors = _ANSI_PAL[ai]
    else:
        colors = (image_rgb & quant_mask).astype(np.uint8)
    # Glyph atlas lookup: (H, W, ch, cw) → transpose → (H*ch, W*cw) bool mask
    glyph_mask = np.ascontiguousarray(
        glyph_atlas[idx].transpose(0, 2, 1, 3)
    ).reshape(H * ch, W * cw)
    # Colors expansion: (H, W, 3) → (H, ch, W, cw, 3) → (H*ch, W*cw, 3)
    c_exp = np.ascontiguousarray(
        np.broadcast_to(
            colors[:, :, np.newaxis, np.newaxis, :], (H, W, ch, cw, 3)
        ).transpose(0, 2, 1, 3, 4)
    ).reshape(H * ch, W * cw, 3)
    canvas = np.zeros((H * ch, W * cw, 3), dtype=np.uint8)
    canvas[glyph_mask] = c_exp[glyph_mask]
    return _PILImage.fromarray(canvas)


def export_to_file(source_path, width, mode, charset_name, output_path, gif_frames=None, color_bits=5):
    """Export ASCII art to output_path.
    Supported extensions: .gif  .mp4  .png  .jpg  .html
    gif_frames: list of (bgr_array, duration_s) when source is an animated GIF.
    """
    if not _PIL_AVAILABLE:
        print("Error: Pillow required for export. Install with: pip install pillow")
        return
    ext = os.path.splitext(output_path)[1].lower()
    if ext not in {".gif", ".mp4", ".png", ".jpg", ".jpeg", ".html"}:
        print(f"Unsupported format '{ext}'. Supported: .gif .mp4 .png .jpg .html")
        return

    atlas, cw, ch = _init_ascii_renderer(charset_name)

    # Detect static image sources (cv2.VideoCapture can't decode them)
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}
    _is_image = (
        gif_frames is None
        and source_path
        and os.path.splitext(source_path)[1].lower() in _IMAGE_EXTS
    )

    # Resolve frame source
    cap = None
    raw_frames = None
    fps_out = 25.0
    dur_ms_list = [100]
    total = 1

    if gif_frames is not None:
        raw_frames = [f[0] for f in gif_frames]
        dur_ms_list = [max(int(f[1] * 1000), 20) for f in gif_frames]
        fps_out = 1000.0 / dur_ms_list[0] if dur_ms_list else 10.0
        total = len(raw_frames)
    elif _is_image:
        bgr = cv2.imread(source_path)
        if bgr is None:
            print(f"Error: Could not read image '{source_path}'.")
            return
        raw_frames = [bgr]
        dur_ms_list = [100]
        total = 1
    else:
        cap = cv2.VideoCapture(source_path)
        if not cap.isOpened():
            print(f"Error: Could not open '{source_path}'.")
            return
        fps_out = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur_ms_list = [int(1000 / fps_out)]

    print(f"Exporting {total if total > 0 else '?'} frame(s) -> '{output_path}'")

    def _next_frame():
        """Yield BGR frames from either raw_frames list or cap."""
        if raw_frames is not None:
            yield from raw_frames
        else:
            while True:
                ret, bgr = cap.read()
                if not ret:
                    break
                yield bgr

    def _dur(i):
        return dur_ms_list[i] if i < len(dur_ms_list) else dur_ms_list[0]

    # PNG / JPG: single first frame
    if ext in {".png", ".jpg", ".jpeg"}:
        bgr = next(_next_frame())
        _render_frame_to_pil(resize_image(bgr, width), mode, charset_name, atlas, cw, ch, color_bits).save(output_path)
        if cap:
            cap.release()
        print(f"Saved: {output_path}")
        return

    # HTML: JS-animated HTML, one <div> per frame
    if ext == ".html":
        if total > 150:
            print(f"  Warning: {total} frames may produce a large HTML file. Consider .mp4.")
        frames_html, delays = [], []
        for i, bgr in enumerate(_next_frame()):
            frames_html.append(pixels_to_html(resize_image(bgr, width), mode, charset_name))
            delays.append(_dur(i))
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{total}...", end="\r", flush=True)
        if cap:
            cap.release()
        frame_divs = "".join(f'<div class="frame">{f}</div>' for f in frames_html)
        html = (
            '<!DOCTYPE html><html><head><meta charset="UTF-8"><style>'
            'body{background:#0c0c0c;margin:0;display:flex;justify-content:center;}'
            '.frame{display:none;white-space:nowrap;font-family:monospace;font-size:8px;line-height:9px;letter-spacing:0;}'
            '.active{display:block;}'
            f'</style></head><body><div id="v">{frame_divs}</div><script>'
            'const f=document.querySelectorAll(".frame");'
            f'const d={delays};'
            'let i=0;function n(){f[i].classList.remove("active");i=(i+1)%f.length;'
            'f[i].classList.add("active");setTimeout(n,d[i]||100);}'
            'if(f.length){f[0].classList.add("active");setTimeout(n,d[0]||100);}'
            '</script></body></html>'
        )
        print(f"\nSaving HTML...")
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"Saved: {output_path}")
        return

    # GIF: load all frames into memory then save
    if ext == ".gif":
        if total > 200:
            print(f"  Warning: {total} frames may produce a very large GIF. Consider .mp4.")
        pil_frames, frame_durations = [], []
        for i, bgr in enumerate(_next_frame()):
            pil_frames.append(
                _render_frame_to_pil(resize_image(bgr, width), mode, charset_name, atlas, cw, ch, color_bits)
            )
            frame_durations.append(_dur(i))
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{total} frames...", end="\r", flush=True)
        if cap:
            cap.release()
        if pil_frames:
            print(f"\nSaving GIF...")
            pil_frames[0].save(
                output_path, save_all=True, append_images=pil_frames[1:],
                duration=frame_durations, loop=0, optimize=False,
            )
        print(f"Saved: {output_path}")
        return

    # MP4: streaming render (avoids loading all frames into RAM)
    # Audio is muxed in afterwards via ffmpeg (if available)
    if ext == ".mp4":
        # Write silent video to a temp file first so we can mux audio after
        tmp_fd, tmp_silent = tempfile.mkstemp(suffix="_silent.mp4")
        os.close(tmp_fd)
        try:
            vw = None
            for i, bgr in enumerate(_next_frame()):
                pil_frame = _render_frame_to_pil(resize_image(bgr, width), mode, charset_name, atlas, cw, ch, color_bits)
                if vw is None:
                    out_w, out_h = pil_frame.size
                    vw = cv2.VideoWriter(
                        tmp_silent, cv2.VideoWriter_fourcc(*"mp4v"), fps_out, (out_w, out_h)
                    )
                vw.write(cv2.cvtColor(np.array(pil_frame), cv2.COLOR_RGB2BGR))
                if (i + 1) % 50 == 0:
                    print(f"  {i + 1}/{total if total > 0 else '?'} frames...", end="\r", flush=True)
            if cap:
                cap.release()
            if vw:
                vw.release()
            print()
            # Try to mux the original audio track using the bundled ffmpeg binary
            audio_added = False
            if gif_frames is None and not _is_image and source_path:
                try:
                    subprocess.run(
                        [
                            _FFMPEG_EXE, "-y",
                            "-i", tmp_silent,
                            "-i", source_path,
                            "-c:v", "copy",
                            "-c:a", "aac", "-b:a", "192k",
                            "-map", "0:v:0", "-map", "1:a:0",
                            "-shortest",
                            output_path,
                        ],
                        check=True,
                        capture_output=True,
                        timeout=600,
                    )
                    audio_added = True
                    print("Audio track added.")
                except FileNotFoundError:
                    print("ffmpeg binary not found — saving without audio.")
                except subprocess.CalledProcessError:
                    print("Could not add audio (source may have no audio track) — saving without audio.")
                except subprocess.TimeoutExpired:
                    print("ffmpeg timed out — saving without audio.")
            if audio_added:
                os.remove(tmp_silent)
            else:
                os.replace(tmp_silent, output_path)
            print(f"Saved: {output_path}")
        except Exception:
            try:
                os.remove(tmp_silent)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# CLI utility commands
# ---------------------------------------------------------------------------

def cmd_list_charsets():
    """Print all built-in charsets with a gradient preview bar."""
    print("\n\033[1mAvailable charsets\033[0m  (use with -c NAME or -c \"custom chars\")\n")
    for name, chars in CHARSETS.items():
        n = len(chars)
        # Build a short gradient bar sampling evenly across the charset
        bar_len = 40
        bar = "".join(chars[int(i * (n - 1) / (bar_len - 1))] for i in range(bar_len))
        print(f"  \033[33m{name:<14}\033[0m ({n:>3} chars)  {bar}")
    print()
    print("  \033[2mTip: pass any string directly to create a custom charset\033[0m")
    print("  \033[2mExample: -c \" .:░▒▓█\"\033[0m\n")


def cmd_wizard():
    """Interactive resolution picker based on current terminal size."""
    try:
        term = shutil.get_terminal_size(fallback=(80, 24))
    except Exception:
        term = os.terminal_size((80, 24))
    cols, rows = term.columns, term.lines

    print(f"\n\033[1mTerminal size detected:\033[0m {cols} × {rows}\n")
    print(f"{'Width':>8}  {'Chars (w×h)':^14}  {'bw MB/s':>9}  {'color MB/s':>11}  {'truecol MB/s':>13}  Bar")
    print("  " + "─" * 72)

    widths = []
    for frac, label in [(1.0, "Full"), (0.75, "3/4"), (0.5, "Half"), (0.33, "1/3"), (0.25, "1/4")]:
        w = max(10, int(cols * frac))
        if widths and widths[-1][0] == w:
            continue
        widths.append((w, label))

    for w, label in widths:
        h = int(w * 0.5)  # typical aspect ratio after resize
        total = w * h
        fps = 25
        bw_mbs   = total * 1    * fps / 1e6
        col_mbs  = total * 7    * fps / 1e6   # avg after RLE
        tru_mbs  = total * 12   * fps / 1e6   # avg at --colors 32

        def grade(mbs):
            if mbs < 1.5:
                return "\033[32m✓ safe\033[0m"
            elif mbs < 3.0:
                return "\033[33m~ ok\033[0m "
            else:
                return "\033[31m✗ slow\033[0m"

        bar_full = int(w / cols * 40)
        bar = "█" * bar_full + "░" * (40 - bar_full)

        print(
            f"  {label:<6} {w:>4}  {w:>4}×{h:<4}  "
            f"{bw_mbs:>6.2f} {grade(bw_mbs):>4}  "
            f"{col_mbs:>6.2f} {grade(col_mbs):>4}  "
            f"{tru_mbs:>6.2f} {grade(tru_mbs):>4}  "
            f"{bar}"
        )

    print()
    print("  \033[2mTip: choose a width where all desired modes are within safe range.\033[0m")
    print(f"  \033[2mExample: movie-ascii myvideo.mp4 -w {int(cols * 0.5)} -m truecolor\033[0m\n")


def setup_args():
    _epilog = (
        "Examples:\n"
        "  movie-ascii clip.mp4                        # play in black & white\n"
        "  movie-ascii clip.mp4 -m truecolor -w 120    # truecolor, 120 cols wide\n"
        "  movie-ascii image.jpg --save out.png        # export frame as PNG\n"
        "  movie-ascii anim.gif -m ascii-color         # animated GIF in terminal\n"
        "  movie-ascii https://youtu.be/... -m bw      # stream from YouTube\n"
        "  movie-ascii clip.mp4 -c blocks              # use block charset\n"
        "  movie-ascii clip.mp4 -c \" .:▒█\"            # custom charset string\n"
        "  movie-ascii --list-charsets                 # show all charsets\n"
        "  movie-ascii --wizard                        # pick resolution for your terminal\n"
    )

    parser = argparse.ArgumentParser(
        prog="movie-ascii",
        description="Play videos, GIFs and images as ASCII art in your terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog,
    )

    parser.add_argument(
        "filepath",
        nargs="?",
        metavar="FILE|URL",
        help="Path or URL to a video, GIF, or image file.",
    )

    parser.add_argument(
        "-w", "--width",
        type=int,
        default=None,
        metavar="COLS",
        help="Output width in characters (default: 80%% of terminal width).",
    )

    parser.add_argument(
        "-m", "--mode",
        choices=["bw", "ascii-color", "truecolor"],
        default="bw",
        metavar="MODE",
        help="Render mode: bw | ascii-color | truecolor  (default: bw).",
    )

    parser.add_argument(
        "-c", "--charset",
        default="standard",
        metavar="NAME|CHARS",
        help=(
            "Charset to use.  Built-in names: "
            + ", ".join(CHARSETS.keys())
            + ".  Or pass any custom string (min 2, max 256 chars)."
        ),
    )

    parser.add_argument(
        "--colors",
        type=int,
        default=5,
        choices=range(3, 9),
        metavar="BITS",
        help=(
            "Color quantization depth for truecolor mode (3–8 bits per channel, "
            "default 5).  Lower = faster / less data; higher = more detail."
        ),
    )

    parser.add_argument(
        "-l", "--lang",
        type=str,
        default="es",
        metavar="LANG",
        help="Preferred subtitle language for YouTube videos (e.g. es, en, fr).",
    )

    parser.add_argument(
        "--save",
        type=str,
        default=None,
        metavar="FILE",
        help="Export to file instead of playing.  Supported: .png .jpg .gif .mp4 .html",
    )

    parser.add_argument(
        "--grid",
        action="store_true",
        help="Generate an HTML grid comparing all render modes and open it in a browser.",
    )

    parser.add_argument(
        "-L", "--list-charsets",
        action="store_true",
        help="List all built-in charsets with a preview and exit.",
    )

    parser.add_argument(
        "--wizard",
        action="store_true",
        help="Show a resolution guide based on your terminal size and exit.",
    )

    return parser.parse_args()


def resize_image(image, new_width=100):
    height, width = image.shape[:2]
    if width == 0 or height == 0:
        return image
    aspect_ratio = height / width
    new_height = max(1, int(new_width * aspect_ratio * 0.5))
    return cv2.resize(image, (new_width, new_height))


def pixels_to_text(image, mode, charset_name, color_bits=5):
    """Generates text for the terminal (with ANSI codes).
    color_bits: 3-8, controls truecolor quantization (5=default, lower=more RLE compression).
    """
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    chars = CHARSETS[charset_name]
    n_chars = len(chars)

    # Vectorized luminance and char-index computation (avoids per-pixel Python loop)
    lum = (
        0.299 * image_rgb[:, :, 0].astype(np.float32)
        + 0.587 * image_rgb[:, :, 1].astype(np.float32)
        + 0.114 * image_rgb[:, :, 2].astype(np.float32)
    )
    idx = np.clip((lum * ((n_chars - 1) / 255.0)).astype(np.int32), 0, n_chars - 1)

    if mode == "bw":
        char_arr = np.array(list(chars))
        return "\n".join("".join(char_arr[row].tolist()) for row in idx) + "\n"

    elif mode == "ascii-color":
        r, g, b = image_rgb[:, :, 0], image_rgb[:, :, 1], image_rgb[:, :, 2]
        ansi = (
            (r > 127).astype(np.int32)
            + (g > 127).astype(np.int32) * 2
            + (b > 127).astype(np.int32) * 4
        )
        lines = []
        for ri in range(idx.shape[0]):
            row_ansi = ansi[ri].tolist()
            row_idx = idx[ri].tolist()
            parts = []
            prev_ai = -1
            for ci in range(len(row_idx)):
                ai = row_ansi[ci]
                ch = chars[row_idx[ci]]
                if ai != prev_ai:
                    parts.append(f"\033[{30 + ai}m{ch}")
                    prev_ai = ai
                else:
                    parts.append(ch)
            parts.append("\033[0m")
            lines.append("".join(parts))
        return "\n".join(lines) + "\n"

    else:  # truecolor — RLE with configurable quantization
        # color_bits controls how many bits per channel are kept.
        # Lower = larger RLE runs = less data → safer for slow terminals.
        # 5 bits (mask 0xF8) is the stable default; 8 bits = full precision.
        quant_mask = 0xFF & ~((1 << (8 - color_bits)) - 1)
        char_list = list(chars)
        r_ch = image_rgb[:, :, 0]
        g_ch = image_rgb[:, :, 1]
        b_ch = image_rgb[:, :, 2]
        lines = []
        for ri in range(idx.shape[0]):
            r_row = r_ch[ri].tolist()
            g_row = g_ch[ri].tolist()
            b_row = b_ch[ri].tolist()
            idx_row = idx[ri].tolist()
            parts = []
            prev_r = prev_g = prev_b = -1
            for ci in range(len(idx_row)):
                rv = r_row[ci] & quant_mask
                gv = g_row[ci] & quant_mask
                bv = b_row[ci] & quant_mask
                ch = char_list[idx_row[ci]]
                if rv != prev_r or gv != prev_g or bv != prev_b:
                    parts.append(f"\033[38;2;{rv};{gv};{bv}m{ch}")
                    prev_r, prev_g, prev_b = rv, gv, bv
                else:
                    parts.append(ch)
            parts.append("\033[0m")
            lines.append("".join(parts))
        return "\n".join(lines) + "\n"


def pixels_to_html(image, mode, charset_name):
    """Generates HTML code for the comparative grid."""
    parts = []
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    chars = CHARSETS[charset_name]
    n_chars = len(chars)

    ansi_colors = [
        "#555555",
        "#FF5555",
        "#55FF55",
        "#FFFF55",
        "#5555FF",
        "#FF55FF",
        "#55FFFF",
        "#FFFFFF",
    ]

    for row in image_rgb:
        for pixel in row:
            r, g, b = int(pixel[0]), int(pixel[1]), int(pixel[2])
            luminance = int(0.299 * r + 0.587 * g + 0.114 * b)
            char_index = min(int(luminance / 255 * (n_chars - 1)), n_chars - 1)
            char = chars[char_index].translate(_HTML_ESCAPE)

            if mode == "bw":
                parts.append(char)
            elif mode == "ascii-color":
                ansi_index = (
                    (1 if r > 127 else 0)
                    + (2 if g > 127 else 0)
                    + (4 if b > 127 else 0)
                )
                parts.append(
                    f'<span style="color:{ansi_colors[ansi_index]}">{char}</span>'
                )
            else:  # truecolor
                parts.append(f'<span style="color:rgb({r},{g},{b})">{char}</span>')
        parts.append("<br>")
    return "".join(parts)


def generate_html_grid(image_path, original_width):
    """Creates an HTML file with the original image and ASCII comparison."""
    print("Generating HTML comparison grid (this may take a few seconds)...")

    # 1. Read pure original image
    original_img = cv2.imread(image_path)
    if original_img is None:
        print(f"Error: Could not load '{image_path}' for the grid.")
        return

    # Convert the original image to Base64 to embed directly in HTML
    _, buffer = cv2.imencode(".png", original_img)
    img_base64 = base64.b64encode(buffer).decode("utf-8")
    original_img_html = f'<img src="data:image/png;base64,{img_base64}" style="max-height: 300px; border-radius: 5px;">'

    # 2. Process image for ASCII
    image_processed = cv2.convertScaleAbs(original_img, alpha=1.2, beta=10)
    grid_width = original_width
    image_resized = resize_image(image_processed, grid_width)

    # 3. CSS: Font size and line-height reduced to 4px to fit without zoom
    css = """
    <style>
    body { background-color: #0c0c0c; color: #f0f0f0; font-family: monospace; margin: 20px;}
    table { border-collapse: collapse; margin-bottom: 40px; }
    td, th { border: 1px solid #333; padding: 15px; text-align: center; vertical-align: middle; }
    th { font-size: 14px; font-family: sans-serif; background-color: #1a1a1a; padding: 15px;}
    .preview-box { white-space: nowrap; display: inline-block; text-align: left; background-color: #000; padding: 10px; border-radius: 5px; font-size: 4px; line-height: 4px; letter-spacing: 0px;}
    h1, h2, p { font-family: sans-serif; }
    h2 { color: #4CAF50; margin-top: 30px;}
    .original-container { background-color: #1a1a1a; padding: 15px; border-radius: 8px; border: 1px solid #333; display: inline-block;}
    </style>
    """

    html_parts = [
        f'<html><head><meta charset="UTF-8">{css}</head><body>',
        f"<h1>Rendering Comparison (Width: {grid_width})</h1>",
        "<h2>0. Original Image</h2>",
        f'<div class="original-container">{original_img_html}</div>',
        "<h2>1. Alphabet-based Modes</h2>",
        "<table><tr><th>Alphabet \\ Mode</th><th>BLACK AND WHITE (bw)</th><th>ASCII COLOR (ascii-color)</th><th>TRUECOLOR (truecolor)</th></tr>",
    ]

    for c in CHARSETS.keys():
        html_parts.append(f"<tr><th>{c.upper()}</th>")
        html_parts.append(f'<td><div class="preview-box">{pixels_to_html(image_resized, "bw", c)}</div></td>')
        html_parts.append(f'<td><div class="preview-box">{pixels_to_html(image_resized, "ascii-color", c)}</div></td>')
        html_parts.append(f'<td><div class="preview-box">{pixels_to_html(image_resized, "truecolor", c)}</div></td>')
        html_parts.append("</tr>")

    html_parts.extend(["</table>", "</body></html>"])
    html = "".join(html_parts)

    file_path = os.path.join(tempfile.gettempdir(), "movie_ascii_preview.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("Done! Opening browser...")
    webbrowser.open(f"file://{file_path}")


def get_youtube_id(url):
    """Extracts the exact video ID from any type of YouTube URL."""
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname or ""
    if hostname in ("youtu.be", "www.youtu.be"):
        video_id = parsed_url.path.lstrip("/").split("/")[0]
        return video_id if video_id else None
    if hostname in (
        "youtube.com", "www.youtube.com",
        "m.youtube.com", "music.youtube.com",
    ):
        if parsed_url.path.startswith("/shorts/"):
            return parsed_url.path.split("/")[2] or None
        return parse_qs(parsed_url.query).get("v", [None])[0]
    return None


def get_subtitles(video_id, target_lang="es"):
    """Downloads subtitles and lets the user choose if the language doesn't exist."""
    if not video_id:
        return None

    print(f"Searching for subtitles for YouTube ID: {video_id}...")
    try:
        try:
            # v1.x compatibility
            api = YouTubeTranscriptApi()
            transcript_list = api.list(video_id)
        except AttributeError:
            # v0.x compatibility
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        try:
            # 1. Try to catch the exact language requested by the user
            transcript = transcript_list.find_transcript([target_lang])
            print(f"  -> Success! Subtitles found in: {transcript.language}")
            return transcript.fetch()

        except Exception:
            # 2. If it fails, extract all available language codes
            available_langs = [t.language_code for t in transcript_list]

            if not available_langs:
                print("  -> Info: There are subtitles, but the format is not compatible.")
                return None

            print(f"\n  [!] Language '{target_lang}' is not available.")
            print(f"  Available languages: {', '.join(available_langs)}")

            # 3. Interactive loop asking the user to choose
            while True:
                choice = (
                    input("  Enter a language code (or press Enter to skip): ")
                    .strip()
                    .lower()
                )

                # If Enter is pressed without typing anything, skip
                if not choice:
                    print("  -> Playing without subtitles.")
                    return None

                # If a valid code is chosen, extract and exit loop
                if choice in available_langs:
                    transcript = transcript_list.find_transcript([choice])
                    print(f"  -> Loading subtitles in: {transcript.language}\n")
                    return transcript.fetch()

                print("  -> Error: Invalid code. Enter one from the list.")

    except Exception as e:
        error_name = type(e).__name__
        if error_name in ["TranscriptsDisabled", "NoTranscriptFound"]:
            print(
                "  -> Info: This video does not have subtitles enabled. Playing without text."
            )
        else:
            print(f"  -> Warning: Could not load subtitles ({error_name}).")
        return None


def get_youtube_stream_url(youtube_url):
    """Extracts the direct URL of the low-resolution video stream."""
    print(f"Connecting to YouTube... Searching for optimal stream for ASCII.")

    # Configure yt-dlp to look for the worst quality/resolution that is still decent
    # 360p or 480p is ideal. Specifically request mp4 so OpenCV doesn't complain.
    ydl_opts = {
        "format": "best[height<=480][ext=mp4]/bestvideo[height<=480]+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(youtube_url, download=False)
            # Get the direct video URL
            stream_url = info_dict.get("url", None)
            return stream_url
    except Exception as e:
        print(f"Error extracting YouTube video: {e}")
        return None


def format_time(seconds):
    if seconds < 0:
        return "00:00"
    total_secs = int(seconds)
    hours, remainder = divmod(total_secs, 3600)
    mins, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def play_gif(filepath, width, mode, charset_name, color_bits=5):
    """Plays an animated GIF as ASCII art in the terminal."""
    if not _PIL_AVAILABLE:
        print("Error: Pillow is required for GIF playback. Run: pip install pillow")
        return
    try:
        gif = _PILImage.open(filepath)
    except Exception as e:
        print(f"Error: Could not open GIF '{filepath}': {e}")
        return

    if not hasattr(gif, 'n_frames') or gif.n_frames < 2:
        # Static image — render a single frame and exit
        frame_bgr = cv2.cvtColor(np.array(gif.convert('RGB')), cv2.COLOR_RGB2BGR)
        resized = resize_image(frame_bgr, width)
        print(pixels_to_text(resized, mode, charset_name, color_bits))
        return

    # Collect all frames and their durations up front
    frames = []
    for i in range(gif.n_frames):
        gif.seek(i)
        duration_ms = gif.info.get('duration', 100)  # default 100ms if missing
        frame_bgr = cv2.cvtColor(np.array(gif.convert('RGB')), cv2.COLOR_RGB2BGR)
        frames.append((frame_bgr, max(duration_ms, 20) / 1000.0))

    _write_queue = _queue.Queue(maxsize=1)
    _writer_stop = threading.Event()

    def _writer():
        CHUNK = 4096
        frame_time = 0.1
        while True:
            try:
                item = _write_queue.get(timeout=0.05)
            except _queue.Empty:
                if _writer_stop.is_set():
                    break
                continue
            if isinstance(item, tuple):
                data, frame_time = item
            else:
                data = item
            offset = 0
            while offset < len(data):
                chunk = data[offset: offset + CHUNK]
                try:
                    _, writable, _ = select.select([], [1], [], frame_time)
                except (OSError, ValueError):
                    return
                if not writable:
                    break
                try:
                    written = os.write(1, chunk)
                    offset += written
                except OSError:
                    return

    _writer_thread = threading.Thread(target=_writer, daemon=True)
    _writer_thread.start()

    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    old_settings = None
    if os.name != "nt":
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    previous_frame_lines = []
    is_running = True
    loop_index = 0
    total_frames = len(frames)

    try:
        while is_running:
            frame_bgr, duration = frames[loop_index]
            loop_index = (loop_index + 1) % total_frames

            # Check for quit key
            if os.name != "nt":
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1).lower()
                    if key == "q":
                        is_running = False
                        break

            resized = resize_image(frame_bgr, width)
            ascii_frame = pixels_to_text(resized, mode, charset_name, color_bits)
            current_frame_lines = ascii_frame.split("\n")

            out = []
            for i, curr_row in enumerate(current_frame_lines):
                if i >= len(previous_frame_lines) or curr_row != previous_frame_lines[i]:
                    out.append(f"\033[{i + 1};1H{curr_row}\033[K")

            controls_str = f" GIF: {total_frames} frames | [Q] Quit "
            controls_y_pos = len(current_frame_lines) + 2
            out.append(
                f"\033[{controls_y_pos};1H\033[36m{controls_str.center(width)[:width]}\033[0m\033[K"
            )

            if out:
                data = "".join(out).encode("utf-8")
                try:
                    _write_queue.put_nowait((data, duration))
                    previous_frame_lines = current_frame_lines
                except _queue.Full:
                    pass

            time.sleep(duration)

    except KeyboardInterrupt:
        pass
    finally:
        _writer_stop.set()
        _writer_thread.join(timeout=2.0)
        if os.name != "nt" and old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h\033[0m\n\n")
        sys.stdout.flush()


def play_video(filepath, width, mode, charset_name, transcript=None, color_bits=5):
    """Plays interactive ASCII video synchronized with Audio and Scrubbing on Pause."""
    set_loglevel("quiet")

    cap = cv2.VideoCapture(filepath)
    if not cap.isOpened():
        print(f"Error: Could not open video '{filepath}'.")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or fps != fps or fps > 240:
        fps = 24.0

    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    total_duration = total_frames / fps if fps > 0 and total_frames > 0 else 0

    audio_player = MediaPlayer(filepath, ff_opts={"vn": True, "sn": True})

    # Writer thread: does blocking I/O without stalling the render loop.
    # The render loop puts encoded frames into the queue and immediately
    # continues. The writer drains the queue at whatever speed the terminal
    # can accept. If the queue is already full the render loop drops the frame
    # (keeps previous_frame_lines so the next diff is correct).
    _write_queue = _queue.Queue(maxsize=1)
    _writer_stop = threading.Event()

    def _writer():
        # PTY kernel buffer on macOS is typically 4096 bytes. os.write() on a
        # blocking fd will block until ALL bytes fit — for a 9 KB frame that means
        # 2-3 kernel round-trips each waiting for the terminal emulator to read.
        # Fix: chunk the frame into ≤4096-byte pieces and gate each chunk with
        # select(timeout=one_frame). If the terminal is congested we drop the
        # remainder of the current frame instead of blocking the writer thread.
        CHUNK = 4096
        one_frame = 1.0 / 25  # generous timeout; updated below from actual fps
        while True:
            try:
                item = _write_queue.get(timeout=0.05)
            except _queue.Empty:
                if _writer_stop.is_set():
                    break
                continue
            if isinstance(item, tuple):
                data, one_frame = item
            else:
                data = item
            offset = 0
            while offset < len(data):
                chunk = data[offset: offset + CHUNK]
                try:
                    _, writable, _ = select.select([], [1], [], one_frame)
                except (OSError, ValueError):
                    return
                if not writable:
                    break  # terminal congested — drop rest of frame
                try:
                    written = os.write(1, chunk)
                    offset += written
                except OSError:
                    return

    _writer_thread = threading.Thread(target=_writer, daemon=True)
    _writer_thread.start()

    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    previous_frame_lines = []
    playback = {"is_paused": False, "is_running": True}

    old_settings = None
    if os.name != "nt":
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    try:
        video_start_time = time.time()
        total_paused_time = 0
        pause_start_time = 0

        while playback["is_running"]:
            key_pressed = False
            force_render = False  # <--- NEW: Controls if we should redraw on pause

            # --- READ KEYBOARD ---
            if os.name != "nt":
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1).lower()
                    if key == "q":
                        playback["is_running"] = False
                        break
                    elif key in [" ", "k"]:
                        playback["is_paused"] = not playback["is_paused"]
                        audio_player.set_pause(playback["is_paused"])
                        key_pressed = True

                    elif key == "l":  # Forward 5s
                        jump_frames = int(5.0 * fps)
                        current_frame = cap.get(cv2.CAP_PROP_POS_FRAMES)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame + jump_frames)
                        video_start_time -= 5.0

                        # Update clock and force rendering even if paused
                        current_pause_duration = (
                            (time.time() - pause_start_time)
                            if (playback["is_paused"] and pause_start_time != 0)
                            else 0
                        )
                        elapsed_time = (
                            (time.time() - video_start_time)
                            - total_paused_time
                            - current_pause_duration
                        )
                        audio_player.seek(elapsed_time, relative=False)
                        previous_frame_lines = []  # Force full redraw after jump
                        force_render = True

                    elif key == "j":  # Backward 5s
                        jump_frames = int(5.0 * fps)
                        current_frame = cap.get(cv2.CAP_PROP_POS_FRAMES)
                        target_frame = max(0, current_frame - jump_frames)
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

                        actual_jump_time = (current_frame - target_frame) / fps
                        video_start_time += actual_jump_time

                        current_pause_duration = (
                            (time.time() - pause_start_time)
                            if (playback["is_paused"] and pause_start_time != 0)
                            else 0
                        )
                        elapsed_time = (
                            (time.time() - video_start_time)
                            - total_paused_time
                            - current_pause_duration
                        )
                        audio_player.seek(elapsed_time, relative=False)
                        previous_frame_lines = []  # Force full redraw after jump
                        force_render = True

            # --- PAUSE LOGIC ---
            if playback["is_paused"]:
                if pause_start_time == 0:
                    pause_start_time = time.time()

                # If we jumped time (J or L), let the loop continue ONCE
                if force_render:
                    pass
                else:
                    # If just paused, update icon to ⏸ instantly
                    if key_pressed and previous_frame_lines:
                        dur_str = (
                            format_time(total_duration)
                            if total_duration > 0
                            else "--:--"
                        )
                        bar_width = max(10, width - 25)

                        current_pause_duration = time.time() - pause_start_time
                        elapsed_time = (
                            (time.time() - video_start_time)
                            - total_paused_time
                            - current_pause_duration
                        )

                        progress = (
                            min(1.0, max(0.0, elapsed_time / total_duration))
                            if total_duration > 0
                            else 0
                        )
                        filled = int(bar_width * progress)
                        empty = bar_width - filled
                        progress_bar = f"[{'█' * filled}{'░' * empty}]"

                        status_bar = f" ⏸  {format_time(elapsed_time)} / {dur_str} {progress_bar} "
                        status_y_pos = len(previous_frame_lines) + 4
                        pause_data = (
                            f"\033[{status_y_pos};1H\033[47;30m{status_bar.center(width)[:width]}\033[0m\033[K"
                        ).encode("utf-8")
                        try:
                            _write_queue.put_nowait(pause_data)
                        except _queue.Full:
                            pass
                    continue
            else:
                if pause_start_time != 0:
                    total_paused_time += time.time() - pause_start_time
                    pause_start_time = 0

            # --- VIDEO ENGINE WITH DYNAMIC CLOCK ---
            current_pause_duration = (
                (time.time() - pause_start_time)
                if (playback["is_paused"] and pause_start_time != 0)
                else 0
            )
            elapsed_time = (
                (time.time() - video_start_time)
                - total_paused_time
                - current_pause_duration
            )

            expected_frame_idx = int(elapsed_time * fps)
            current_frame_idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES))

            # If we are forcing a render, skip wait rules to draw NOW
            if not force_render:
                if current_frame_idx > expected_frame_idx:
                    time.sleep(0.001)
                    continue

                # Skip frames without decoding when we're behind (cap.grab is much
                # cheaper than cap.read since it avoids full frame decompression)
                frames_to_skip = expected_frame_idx - current_frame_idx - 1
                if frames_to_skip > 0:
                    for _ in range(frames_to_skip):
                        if not cap.grab():
                            playback["is_running"] = False
                            break
                    if not playback["is_running"]:
                        break

            ret, frame = cap.read()
            if not ret:
                break

            resized = resize_image(frame, width)
            ascii_frame = pixels_to_text(resized, mode, charset_name, color_bits)
            current_frame_lines = ascii_frame.split("\n")

            out = []
            for i, curr_row in enumerate(current_frame_lines):
                if (
                    i >= len(previous_frame_lines)
                    or curr_row != previous_frame_lines[i]
                ):
                    out.append(f"\033[{i + 1};1H{curr_row}\033[K")

            if transcript:
                current_subtitle = ""
                lo, hi = 0, len(transcript) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    sub = transcript[mid]
                    sub_start = sub["start"] if isinstance(sub, dict) else sub.start
                    sub_duration = sub["duration"] if isinstance(sub, dict) else sub.duration
                    if sub_start + sub_duration < elapsed_time:
                        lo = mid + 1
                    elif sub_start > elapsed_time:
                        hi = mid - 1
                    else:
                        sub_text = sub["text"] if isinstance(sub, dict) else sub.text
                        current_subtitle = sub_text.replace("\n", " ")
                        break

                sub_y_pos = len(current_frame_lines) + 2
                formatted_sub = current_subtitle.center(width)
                out.append(
                    f"\033[{sub_y_pos};1H\033[93m\033[1m{formatted_sub}\033[0m\033[K"
                )

            # --- UNIFIED UI ---
            dur_str = format_time(total_duration) if total_duration > 0 else "--:--"
            bar_width = max(10, width - 25)
            if total_duration > 0:
                progress = min(1.0, max(0.0, elapsed_time / total_duration))
                filled = int(bar_width * progress)
                empty = bar_width - filled
                progress_bar = f"[{'█' * filled}{'░' * empty}]"
            else:
                progress_bar = f"[{'░' * bar_width}]"

            # Icon changes automatically if paused (even during force_render)
            state_icon = "⏸" if playback["is_paused"] else "▶"
            status_bar = f" {state_icon}  {format_time(elapsed_time)} / {dur_str} {progress_bar} "
            status_y_pos = len(current_frame_lines) + 4
            formatted_status = status_bar.center(width)[:width]
            out.append(
                f"\033[{status_y_pos};1H\033[47;30m{formatted_status}\033[0m\033[K"
            )

            controls_str = (
                " Controls: [Space/K] Pause | [J] -5s | [L] +5s | [Q] Quit "
            )
            controls_y_pos = status_y_pos + 1
            formatted_controls = controls_str.center(width)[:width]
            out.append(
                f"\033[{controls_y_pos};1H\033[36m{formatted_controls}\033[0m\033[K"
            )

            if out:
                data = "".join(out).encode("utf-8")
                try:
                    _write_queue.put_nowait((data, 1.0 / fps))
                    previous_frame_lines = current_frame_lines
                except _queue.Full:
                    pass  # Writer busy: drop frame, keep baseline for correct next diff

    except KeyboardInterrupt:
        pass
    finally:
        _writer_stop.set()
        _writer_thread.join(timeout=2.0)

        if os.name != "nt" and old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

        cap.release()
        audio_player.close_player()
        sys.stdout.write("\033[?25h\033[0m\n\n")
        sys.stdout.flush()
        print(f"\033[{len(previous_frame_lines) + 6};1HEnd of playback.")


def main():
    args = setup_args()

    # --- Utility commands (no filepath required) ---
    if args.list_charsets:
        cmd_list_charsets()
        return

    if args.wizard:
        cmd_wizard()
        return

    # --- Resolve charset: built-in name or inline custom string ---
    if args.charset in CHARSETS:
        charset_name = args.charset
    else:
        custom = args.charset
        if len(custom) < 2:
            print("Error: custom charset must have at least 2 characters.", file=sys.stderr)
            sys.exit(1)
        if len(custom) > 256:
            print("Error: custom charset must not exceed 256 characters.", file=sys.stderr)
            sys.exit(1)
        CHARSETS["_custom"] = custom
        charset_name = "_custom"

    # --- Resolve default width from terminal size ---
    color_bits = args.colors
    if args.width is not None:
        width = max(10, args.width)
    else:
        try:
            width = max(40, int(shutil.get_terminal_size(fallback=(100, 24)).columns * 0.8))
        except Exception:
            width = 80

    # --- Require filepath for all playback/export operations ---
    if not args.filepath:
        print("Error: a FILE or URL is required unless using --list-charsets or --wizard.", file=sys.stderr)
        print("Run  movie-ascii --help  for usage.", file=sys.stderr)
        sys.exit(1)

    try:
        if args.grid:
            cap = cv2.VideoCapture(args.filepath)
            ret, frame = cap.read()
            cap.release()
            if not ret:
                print(f"Error: Could not read file '{args.filepath}' for the grid.", file=sys.stderr)
                sys.exit(1)
            tmp_fd, temp_path = tempfile.mkstemp(suffix=".png")
            os.close(tmp_fd)
            try:
                cv2.imwrite(temp_path, frame)
                generate_html_grid(temp_path, width)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            return

        video_source = args.filepath
        transcript = None

        # GIF: use Pillow-based player
        if video_source.lower().endswith(".gif"):
            if args.save:
                if not _PIL_AVAILABLE:
                    print("Error: Pillow is required for GIF export. Run: pip install pillow", file=sys.stderr)
                    sys.exit(1)
                gif = _PILImage.open(video_source)
                gif_frames_export = []
                n_frames = getattr(gif, "n_frames", 1)
                for i in range(n_frames):
                    gif.seek(i)
                    dur = gif.info.get("duration", 100) / 1000.0
                    bgr = cv2.cvtColor(np.array(gif.convert("RGB")), cv2.COLOR_RGB2BGR)
                    gif_frames_export.append((bgr, dur))
                export_to_file(
                    video_source, width, args.mode, charset_name,
                    args.save, gif_frames=gif_frames_export, color_bits=color_bits,
                )
            else:
                play_gif(video_source, width, args.mode, charset_name, color_bits=color_bits)
            return

        # YouTube / streaming URL
        if video_source.startswith("http://") or video_source.startswith("https://"):
            print("Web link detected. Starting streaming protocol...")
            video_id = get_youtube_id(video_source)
            if not args.save:
                transcript = get_subtitles(video_id, target_lang=args.lang)
            video_source = get_youtube_stream_url(video_source)
            if not video_source:
                print("Could not get stream. Aborting.", file=sys.stderr)
                sys.exit(1)

        if args.save:
            export_to_file(video_source, width, args.mode, charset_name, args.save, color_bits=color_bits)
            return

        play_video(video_source, width, args.mode, charset_name, transcript=transcript, color_bits=color_bits)

    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
