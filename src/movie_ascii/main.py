import argparse
import os
import sys
import base64
import webbrowser
import time
from urllib.parse import urlparse, parse_qs
import select
import tty
import termios

if os.name != "nt":
    # Save the original physical error pipe
    original_stderr_fd = os.dup(sys.stderr.fileno())
    # Open a black hole (/dev/null)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    # Redirect the physical pipe to the black hole
    os.dup2(devnull_fd, sys.stderr.fileno())

import cv2
import yt_dlp
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


def setup_args():
    parser = argparse.ArgumentParser(
        description="Converts images and videos to ASCII art."
    )
    parser.add_argument("filepath", help="Path to the image (e.g. test.jpg)")
    parser.add_argument(
        "-w", "--width", type=int, default=100, help="Width in characters"
    )

    parser.add_argument(
        "-c", "--charset", choices=list(CHARSETS.keys()), default="standard"
    )

    parser.add_argument(
        "--grid",
        action="store_true",
        help="Generates and opens an HTML comparing all modes",
    )

    parser.add_argument(
        "-m",
        "--mode",
        choices=["bw", "ascii-color", "truecolor"],
        default="bw",
        help="Mode: 'bw', 'ascii-color' (16 colors), or 'truecolor' (RGB + Alphabet)",
    )

    parser.add_argument(
        "-l",
        "--lang",
        type=str,
        default="es",
        help="Preferred language for subtitles (e.g. 'es', 'en', 'fr')",
    )

    return parser.parse_args()


def resize_image(image, new_width=100):
    height, width = image.shape[:2]
    aspect_ratio = height / width
    new_height = int(new_width * aspect_ratio * 0.5)
    return cv2.resize(image, (new_width, new_height))


def pixels_to_text(image, mode, charset_name):
    """Generates text for the terminal (with ANSI codes)."""
    ascii_str = ""
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    chars = CHARSETS[charset_name]

    for row in image_rgb:
        for pixel in row:
            r, g, b = pixel
            
            luminance = int(0.299 * r + 0.587 * g + 0.114 * b)
            char_index = int(luminance / 255 * (len(chars) - 1))
            char = chars[char_index]

            if mode == "bw":
                ascii_str += char
            elif mode == "ascii-color":
                ansi_index = (
                    (1 if r > 127 else 0)
                    + (2 if g > 127 else 0)
                    + (4 if b > 127 else 0)
                )
                ascii_str += f"\033[{30 + ansi_index}m{char}\033[0m"
            elif mode == "truecolor":
                ascii_str += f"\033[38;2;{r};{g};{b}m{char}\033[0m"

        ascii_str += "\n"
    return ascii_str


def pixels_to_html(image, mode, charset_name):
    """Generates HTML code for the comparative grid."""
    html_str = ""
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    chars = CHARSETS[charset_name]

    # Basic ANSI colors simulated in HEX for ascii-color mode
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
            r, g, b = pixel
            if mode in ["bw", "ascii-color", "truecolor"]:
                luminance = int(0.299 * r + 0.587 * g + 0.114 * b)
                char_index = int(luminance / 255 * (len(chars) - 1))
                char = chars[char_index]
                char = (
                    char.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace(" ", "&nbsp;")
                )

                if mode == "bw":
                    html_str += char
                elif mode == "ascii-color":
                    ansi_index = (
                        (1 if r > 127 else 0)
                        + (2 if g > 127 else 0)
                        + (4 if b > 127 else 0)
                    )
                    html_str += (
                        f'<span style="color: {ansi_colors[ansi_index]};">{char}</span>'
                    )
                elif mode == "truecolor":
                    # Render in HTML with the exact RGB color
                    html_str += f'<span style="color: rgb({r},{g},{b});">{char}</span>'
            elif mode == "block":
                html_str += f'<span style="color: rgb({r},{g},{b});">&#9608;</span>'  # &#9608; is █ in HTML
        html_str += "<br>"
    return html_str


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

    html = f'<html><head><meta charset="UTF-8">{css}</head><body>'
    html += f"<h1>Rendering Comparison (Width: {grid_width})</h1>"

    # --- 0. ORIGINAL IMAGE ---
    html += "<h2>0. Original Image</h2>"
    html += f'<div class="original-container">{original_img_html}</div>'

    # --- 1. MAIN TABLE ---
    html += "<h2>1. Alphabet-based Modes</h2>"
    html += "<table><tr><th>Alphabet \\ Mode</th><th>BLACK AND WHITE (bw)</th><th>ASCII COLOR (ascii-color)</th><th>TRUECOLOR (truecolor)</th></tr>"

    for c in CHARSETS.keys():
        html += f"<tr><th>{c.upper()}</th>"
        html_bw = pixels_to_html(image_resized, "bw", c)
        html += f'<td><div class="preview-box">{html_bw}</div></td>'
        html_color = pixels_to_html(image_resized, "ascii-color", c)
        html += f'<td><div class="preview-box">{html_color}</div></td>'
        html_truecolor = pixels_to_html(image_resized, "truecolor", c)
        html += f'<td><div class="preview-box">{html_truecolor}</div></td>'
        html += "</tr>"

    html += "</table>"

    html += "</body></html>"

    file_path = os.path.abspath("preview.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("Done! Opening browser...")
    webbrowser.open(f"file://{file_path}")


def get_youtube_id(url):
    """Extracts the exact video ID from any type of YouTube URL."""
    parsed_url = urlparse(url)
    if parsed_url.hostname in ("youtu.be", "www.youtu.be"):
        return parsed_url.path[1:]
    if parsed_url.hostname in ("youtube.com", "www.youtube.com"):
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

        except:
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
    mins, secs = divmod(int(seconds), 60)
    return f"{mins:02d}:{secs:02d}"


def play_video(filepath, width, mode, charset_name, transcript=None):
    """Plays interactive ASCII video synchronized with Audio and Scrubbing on Pause."""
    set_loglevel("quiet")

    cap = cv2.VideoCapture(filepath)
    if not cap.isOpened():
        print(f"Error: Could not open video '{filepath}'.")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0 or fps != fps:
        fps = 24.0

    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    total_duration = total_frames / fps if fps > 0 and total_frames > 0 else 0

    audio_player = MediaPlayer(filepath, ff_opts={"vn": True, "sn": True})

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
                        sys.stdout.write(
                            f"\033[{status_y_pos};1H\033[47;30m{status_bar.center(width)[:width]}\033[0m\033[K"
                        )
                        sys.stdout.flush()

                    time.sleep(0.1)
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

            ret, frame = cap.read()
            if not ret:
                break

            if not force_render:
                if current_frame_idx < expected_frame_idx - 1:
                    continue

            resized = resize_image(frame, width)
            ascii_frame = pixels_to_text(resized, mode, charset_name)
            current_frame_lines = ascii_frame.split("\n")

            output = ""
            for i, curr_row in enumerate(current_frame_lines):
                if (
                    i >= len(previous_frame_lines)
                    or curr_row != previous_frame_lines[i]
                ):
                    output += f"\033[{i + 1};1H{curr_row}\033[K"

            if transcript:
                current_subtitle = ""
                for sub in transcript:
                    sub_start = sub["start"] if isinstance(sub, dict) else sub.start
                    sub_duration = (
                        sub["duration"] if isinstance(sub, dict) else sub.duration
                    )
                    sub_text = sub["text"] if isinstance(sub, dict) else sub.text

                    if sub_start <= elapsed_time <= (sub_start + sub_duration):
                        current_subtitle = sub_text.replace("\n", " ")
                        break

                sub_y_pos = len(current_frame_lines) + 2
                formatted_sub = current_subtitle.center(width)
                output += (
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
            output += (
                f"\033[{status_y_pos};1H\033[47;30m{formatted_status}\033[0m\033[K"
            )

            controls_str = (
                " Controls: [Space/K] Pause | [J] -5s | [L] +5s | [Q] Quit "
            )
            controls_y_pos = status_y_pos + 1
            formatted_controls = controls_str.center(width)[:width]
            output += (
                f"\033[{controls_y_pos};1H\033[36m{formatted_controls}\033[0m\033[K"
            )

            if output:
                os.write(1, output.encode("utf-8"))

            previous_frame_lines = current_frame_lines

    except KeyboardInterrupt:
        pass
    finally:
        if os.name != "nt" and old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

        cap.release()
        audio_player.close_player()
        sys.stdout.write("\033[?25h\033[0m\n\n")
        sys.stdout.flush()
        print(f"\033[{len(previous_frame_lines) + 6};1HEnd of playback.")


def main():
    args = setup_args()

    try:
        # Detect if it's a video or an image for the gallery
        if args.grid:
            # If it's the gallery, read the file with VideoCapture to extract only Frame 0
            cap = cv2.VideoCapture(args.filepath)
            ret, frame = cap.read()
            cap.release()

            if not ret:
                print(
                    f"Error: Could not read file '{args.filepath}' for the gallery."
                )
                sys.exit(1)

            # Save that first frame temporarily for generate_html_grid to read
            temp_path = "temp_grid_frame.png"
            cv2.imwrite(temp_path, frame)

            # Call your function intact
            generate_html_grid(temp_path, args.width)

            # Delete the temporary file
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return

        # If NO --grid flag, play the video in the terminal
        video_source = args.filepath
        transcript = None  # Default: no subtitles

        # If we detect it's an internet link...
        if video_source.startswith("http://") or video_source.startswith("https://"):
            print("Web link detected. Starting streaming protocol...")

            # 1. Try to catch subtitles first!
            video_id = get_youtube_id(video_source)
            transcript = get_subtitles(video_id, target_lang=args.lang)

            video_source = get_youtube_stream_url(video_source)

            if not video_source:
                print("Could not get stream. Aborting.")
                sys.exit(1)

        # Pass the local file OR YouTube stream to our engine
        play_video(
            video_source, args.width, args.mode, args.charset, transcript=transcript
        )

    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    main()
