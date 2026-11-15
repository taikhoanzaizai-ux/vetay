from pathlib import Path
import argparse
import base64
import hashlib
import json
import sys
import urllib.request
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "Data"
VERSION = "0.2.28"
PATCH_ABI = "vetay-windows-026"
PATCH_ROOT = "https://raw.githubusercontent.com/taikhoanzaizai-ux/vetay/"

if (ROOT / "ffmpeg.exe").is_file():
    os.environ["FFMPEG_BINARY"] = str(ROOT / "ffmpeg.exe")
LOCK = threading.Lock()
LAST_ANALYSIS = {}
VIDEO_SIZES = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "4:3": (1440, 1080),
    "21:9": (2520, 1080),
}

def prepare_data():
    DATA.mkdir(parents=True, exist_ok=True)
    temp = DATA / "Temp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    os.environ["GRADIO_TEMP_DIR"] = str(temp / "gradio")
    os.environ["HF_HOME"] = str(DATA / "huggingface")

    local = os.environ.get("LOCALAPPDATA")
    marker = DATA / ".migration-complete"
    if not local or marker.exists():
        return
    legacy = Path(local) / "VeTay"
    if legacy.resolve() == DATA.resolve() or not legacy.is_dir():
        return
    files = list(legacy.glob("VeTay-*.mp4"))
    for name in ("models", "chrome-extension", "huggingface"):
        folder = legacy / name
        if folder.is_dir():
            files.extend(file for file in folder.rglob("*") if file.is_file())
    for source in files:
        target = DATA / source.relative_to(legacy)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as staging:
            copy = Path(staging) / source.name
            shutil.copy2(source, copy)
            if not target.exists():
                copy.replace(target)
    marker.touch()


def engine():
    path = ROOT / "engine"
    if not (path / "handdraw" / "render.py").is_file():
        raise RuntimeError("Bộ render Whiteboard Animation VI bị thiếu trong bản cài.")
    sys.path.insert(0, str(path))
    from handdraw.render import render_video
    from handdraw.settings import Settings
    return path, render_video, Settings

def ffmpeg(*args):
    exe = os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("Không tìm thấy FFmpeg. Cài FFmpeg hoặc đặt FFMPEG_BINARY.")
    return subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          check=True, capture_output=True)

def duration(path):
    import wave
    with wave.open(str(path), "rb") as track:
        return track.getnframes() / track.getframerate()

def transcribe(voice):
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8", download_root=str(DATA / "models"))
    segments, _ = model.transcribe(str(voice), language="vi", vad_filter=True, word_timestamps=True)
    return [(float(word.start), float(word.end), word.word)
            for segment in segments for word in (segment.words or ()) if word.word.strip()]

def scenes(words, seconds):
    if not words or seconds <= 0:
        raise ValueError("Không tìm thấy lời đọc hoặc âm thanh trống.")
    blocks = [(0.0, [])]
    previous_end = 0.0
    previous_word = ""
    for start, end, word in words:
        scene_start, current = blocks[-1]
        elapsed = start - scene_start
        pause = start - previous_end
        sentence_end = bool(re.search(r'[.!?…]+[)"”\']*$', previous_word.rstrip()))
        can_cut = current and seconds - start >= 2
        if can_cut and (elapsed >= 14 or (elapsed >= 4.5 and pause >= .55)
                        or (elapsed >= 6.5 and sentence_end)):
            current = []
            blocks.append((start, current))
        current.append(word)
        previous_end = end
        previous_word = word
    end_frame = round(seconds * 24)
    result = []
    for i, (start, words) in enumerate(blocks):
        first = round(start * 24)
        last = round(blocks[i+1][0] * 24) if i+1 < len(blocks) else end_frame
        text = "".join(words).strip()
        # Whiteboard Animation VI accepts up to 120 seconds for a single render.
        while last - first > 120 * 24:
            result.append((first, first + 120 * 24, text))
            first += 120 * 24
        if last > first:
            result.append((first, last, text))
    return result

def prompt_for(text, ratio="9:16"):
    rules = (
        (r"cà phê|ăn vặt|đồ uống", "ly cà phê và món ăn nhẹ đặt cạnh ví tiền"),
        (r"tiết kiệm|dành dụm", "hũ tiết kiệm, bàn tay bỏ đồng xu vào hũ"),
        (r"nhón gót|bắp chân", "người đang nhón gót, góc nhìn ngang dễ thấy bàn chân"),
        (r"thanh toán|chi tiêu|chi phí", "ví tiền, hóa đơn và vài đồng xu"),
        (r"một năm|mỗi tháng|mỗi tuần|365 ngày", "lịch để bàn và mũi tên chỉ thời gian"),
    )
    visuals = next((art for key, art in rules if re.search(key, text, re.I)), "")
    direction = (f"Gợi ý hình ảnh trọng tâm: {visuals}. " if visuals else
                 "Chọn một chủ thể và một hành động cụ thể có trong lời đọc. ")
    return (f"Tạo một ảnh minh họa tỷ lệ {ratio} để chuyển thành video vẽ tay. "
            "Nền trắng trơn, đường viền đậm rõ, ít chi tiết, chủ thể tách nền, "
            "màu xanh ngọc và vàng nhạt. Không chữ, số, logo hoặc watermark. "
            + direction + "Dùng lời đọc sau làm ngữ cảnh, không chép thành chữ trên ảnh: " + text)

def timecode(frame):
    ms = round(frame * 1000 / 24)
    minutes, remainder = divmod(ms, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{minutes:02}:{seconds:02}.{fraction:03}"

def scene_rows(plan, ratio="9:16"):
    return [[i, timecode(start), timecode(end), round((end-start)/24, 3), text, prompt_for(text, ratio)]
            for i, (start, end, text) in enumerate(plan, 1)]

def prepare_audio(script, voice, folder):
    wav = folder / "voice.wav"
    if not voice:
        if not script or not script.strip():
            raise ValueError("Nhập kịch bản hoặc tải bản thu.")
        import edge_tts
        speech = folder / "speech.mp3"
        asyncio.run(edge_tts.Communicate(script.strip(), "vi-VN-NamMinhNeural").save(str(speech)))
        voice = speech
    ffmpeg("-i", voice, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav)
    return wav

def render_folder(voice, pictures, plan, destination, report, ratio="9:16"):
    from PIL import Image, ImageOps
    import numpy as np
    path, render_video, Settings = engine()
    width, height = VIDEO_SIZES[ratio]
    preview_width, preview_height = ((size * 2 // 5) // 2 * 2 for size in (width, height))
    if len(pictures) < len(plan):
        raise ValueError(f"Cần {len(plan)} ảnh, hiện có {len(pictures)}.")
    with tempfile.TemporaryDirectory(prefix="vetay-", dir=destination.parent) as work:
        work = Path(work)
        clips = []
        for i, ((start, end, _), picture) in enumerate(zip(plan, pictures)):
            report(f"Whiteboard Animation VI: cảnh {i+1}/{len(plan)}…")
            with Image.open(picture) as raw:
                art = ImageOps.contain(ImageOps.exif_transpose(raw).convert("RGB"), (preview_width, preview_height))
            canvas = Image.new("RGB", (preview_width, preview_height), "white")
            canvas.paste(art, ((preview_width-art.width)//2, (preview_height-art.height)//2))
            settings = Settings(device="cpu", encoder="cpu", width=preview_width, height=preview_height, fps=24, fit="contain",
                                duration=(end-start)/24, draw_percent=70, mode="e",
                                subject_first=(path / "assets/v181d/model.safetensors").is_file())
            clip, _ = render_video(np.array(canvas), settings, output_dir=work)
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
        output = work / "complete.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-i", voice, "-map", "0:v", "-map", "1:a",
               "-vf", f"scale={width}:{height}:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-af", "apad", "-shortest", "-movflags", "+faststart", output)
        os.replace(output, destination)
    return str(destination)

def patch_read(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "VeTay-Windows", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Gói cập nhật vượt kích thước cho phép.")
    return data

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Số phiên bản không hợp lệ.")
    return tuple(map(int, value.split(".")))

BUNDLED_EXTENSION = {
    "version": "1.3.14",
    "commit": "d9ac86931ab3e93a360db3a29cfec544cc4f7f93",
    "files": {
        "flow.js": "d626ab1e5efea62f811a979ea4a8ad24e74c6146",
        "worker.js": "c18bb356679c6c418b255cbeca62536bff55cbcc",
        "manifest.json": "852f8c0030b2b71b7f02e46ec8c7a131c2d6e0cb",
        "popup.html": "d1f3fe3d89500eb7788fc312842525a9c22193c4"
    }
}
BUNDLED_EXTENSION_FILES = {
    "flow.js": "KCgpID0+IHsKICBjb25zdCBwYWlyaW5nID0gL14jdmV0YXk9KFthLWYwLTldezMyfSkkLy5leGVjKGxvY2F0aW9uLmhhc2gpPy5bMV07CiAgaWYgKHdpbmRvdy5fX3ZlVGF5UnVubmluZykgcmV0dXJuOwogIHdpbmRvdy5fX3ZlVGF5UnVubmluZyA9IHRydWU7CiAgY29uc3Qgc2xlZXAgPSAobXMpID0+IG5ldyBQcm9taXNlKHJlc29sdmUgPT4gc2V0VGltZW91dChyZXNvbHZlLG1zKSk7CiAgY29uc3QgdmlzaWJsZSA9IG5vZGUgPT4gbm9kZS5nZXRDbGllbnRSZWN0cygpLmxlbmd0aCAmJiBnZXRDb21wdXRlZFN0eWxlKG5vZGUpLnZpc2liaWxpdHkgIT09ICJoaWRkZW4iOwogIGNvbnN0IHNlbmQgPSBhc3luYyAobWVzc2FnZSkgPT4gewogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBjaHJvbWUucnVudGltZS5zZW5kTWVzc2FnZShtZXNzYWdlKTsKICAgIGlmICghcmVzcG9uc2U/Lm9rKSB0aHJvdyBFcnJvcihyZXNwb25zZT8uZXJyb3IgfHwgIk3huqV0IGvhur90IG7hu5FpIHbhu5tpIFbhur0gVGF5LiIpOwogICAgcmV0dXJuIHJlc3BvbnNlLnJlc3VsdDsKICB9OwogIGZ1bmN0aW9uIHJlc3VsdEltYWdlcyhjYXJkKSB7CiAgICByZXR1cm4gWy4uLmNhcmQucXVlcnlTZWxlY3RvckFsbCgiaW1nIildLmZpbHRlcihpbWcgPT4gewogICAgICBpZiAoIWltZy5jb21wbGV0ZSB8fCAhaW1nLm5hdHVyYWxXaWR0aCB8fCAhaW1nLm5hdHVyYWxIZWlnaHQgfHwgIXZpc2libGUoaW1nKSkgcmV0dXJuIGZhbHNlOwogICAgICBjb25zdCBzcmMgPSBpbWcuY3VycmVudFNyYyB8fCBpbWcuc3JjOwogICAgICBjb25zdCB1cmwgPSBuZXcgVVJMKHNyYywgbG9jYXRpb24uaHJlZik7CiAgICAgIGNvbnN0IG1lZGlhID0gL14ow5QgaGnhu4NuIHRo4buLIGjDrG5oIOG6o25oIGPhu6dhIG5nxrDhu51pIGTDuW5nfFVzZXIgaW1hZ2UgdGlsZSkkL2l1LnRlc3QoaW1nLmFsdC50cmltKCkpOwogICAgICBjb25zdCBvcmlnaW5hbCA9IC9eKGJsb2I6fGh0dHBzOlwvXC9bXi9dKlwuZ29vZ2xldXNlcmNvbnRlbnRcLmNvbVwvKS9pLnRlc3Qoc3JjKTsKICAgICAgcmV0dXJuIG9yaWdpbmFsIHx8IChtZWRpYSAmJiAodXJsLm9yaWdpbiA9PT0gbG9jYXRpb24ub3JpZ2luIHx8IHVybC5wcm90b2NvbCA9PT0gImRhdGE6IikpOwogICAgfSk7CiAgfQogIGNvbnN0IGNhcHRpb24gPSBidXR0b24gPT4gKGJ1dHRvbi5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBidXR0b24uZ2V0QXR0cmlidXRlKCJ0aXRsZSIpIHx8IGJ1dHRvbi50ZXh0Q29udGVudCB8fCAiIikudHJpbSgpOwogIGZ1bmN0aW9uIHByb21wdEZpZWxkKCkgewogICAgY29uc3QgZmllbGRzID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ3RleHRhcmVhLFtjb250ZW50ZWRpdGFibGU9InRydWUiXSxbY29udGVudGVkaXRhYmxlPSJwbGFpbnRleHQtb25seSJdJyldCiAgICAgIC5maWx0ZXIoZmllbGQgPT4gdmlzaWJsZShmaWVsZCkgJiYgIWZpZWxkLmNsb3Nlc3QoJ1tpbmVydF0sW2FyaWEtaGlkZGVuPSJ0cnVlIl0sW3JvbGU9InNlYXJjaCJdJykKICAgICAgICAmJiAhZmllbGQuZGlzYWJsZWQgJiYgIWZpZWxkLnJlYWRPbmx5ICYmIGZpZWxkLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpICE9PSAidHJ1ZSIpOwogICAgY29uc3QgbGFiZWxzID0gZmllbGQgPT4gWyJhcmlhLWxhYmVsIiwgInBsYWNlaG9sZGVyIiwgImRhdGEtcGxhY2Vob2xkZXIiLCAidGl0bGUiXQogICAgICAubWFwKG5hbWUgPT4gZmllbGQuZ2V0QXR0cmlidXRlKG5hbWUpIHx8ICIiKS5qb2luKCIgIik7CiAgICBjb25zdCBlbGlnaWJsZSA9IGZpZWxkcy5maWx0ZXIoZmllbGQgPT4gIS9zZWFyY2h8dMOsbSBraeG6v218cmVuYW1lfMSR4buVaSB0w6pufHByb2plY3QgbmFtZXx0w6puIGThu7Egw6FuL2kudGVzdChsYWJlbHMoZmllbGQpKQogICAgICAmJiAhZmllbGRzLnNvbWUocGFyZW50ID0+IHBhcmVudCAhPT0gZmllbGQgJiYgcGFyZW50LmNvbnRhaW5zKGZpZWxkKSkpOwogICAgY29uc3QgbWF0Y2hlcyA9IGVsaWdpYmxlLmZpbHRlcihmaWVsZCA9PiAvcHJvbXB0fHdoYXQgKD86ZG98d291bGQpIHlvdXxkZXNjcmliZXxtw7QgdOG6o3xi4bqhbiBtdeG7kW58dOG6oW8gbuG7mWkgZHVuZy9pLnRlc3QobGFiZWxzKGZpZWxkKSkpOwogICAgaWYgKG1hdGNoZXMubGVuZ3RoID09PSAxKSByZXR1cm4gbWF0Y2hlc1swXTsKICAgIHJldHVybiBlbGlnaWJsZS5sZW5ndGggPT09IDEgPyBlbGlnaWJsZVswXSA6IG51bGw7CiAgfQogIGNvbnN0IHJhdGlvTGFiZWwgPSByYXRpbyA9PiBuZXcgUmVnRXhwKCIoPzpefFteMC05XSkiICsgcmF0aW8ucmVwbGFjZSgiOiIsICJcXHMqWzriiLZdXFxzKiIpICsgIig/IVswLTldKSIsICJ1Iik7CiAgYXN5bmMgZnVuY3Rpb24gc2VsZWN0UmF0aW8ocmF0aW8pIHsKICAgIGNvbnN0IHNlbGVjdGVkID0gbm9kZSA9PiBbImFyaWEtY2hlY2tlZCIsICJhcmlhLXNlbGVjdGVkIiwgImFyaWEtcHJlc3NlZCJdLnNvbWUoa2V5ID0+IG5vZGUuZ2V0QXR0cmlidXRlKGtleSkgPT09ICJ0cnVlIikKICAgICAgfHwgbm9kZS5nZXRBdHRyaWJ1dGUoImRhdGEtc3RhdGUiKSA9PT0gImNoZWNrZWQiIHx8IG5vZGUuZ2V0QXR0cmlidXRlKCJkYXRhLXN0YXRlIikgPT09ICJhY3RpdmUiOwogICAgY29uc3QgY29udHJvbHMgPSAoKSA9PiBbLi4uZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiYnV0dG9uLFtyb2xlPWJ1dHRvbl0sW3JvbGU9cmFkaW9dLFtyb2xlPW9wdGlvbl0sW3JvbGU9bWVudWl0ZW1yYWRpb10sW3JvbGU9dGFiXSIpXQogICAgICAuZmlsdGVyKG5vZGUgPT4gdmlzaWJsZShub2RlKSAmJiAhbm9kZS5kaXNhYmxlZCAmJiBub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1kaXNhYmxlZCIpICE9PSAidHJ1ZSIpOwogICAgY29uc3QgbGFiZWxzID0gbm9kZSA9PiBbY2FwdGlvbihub2RlKSwgbm9kZS5nZXRBdHRyaWJ1dGUoImRhdGEtdmFsdWUiKSB8fCAiIiwgbm9kZS5pbm5lclRleHQgfHwgIiJdLmpvaW4oIiAiKTsKICAgIGNvbnN0IGV4YWN0ID0gcmF0aW9MYWJlbChyYXRpbyk7CiAgICBjb25zdCBoYXNSYXRpbyA9IHJhdGlvTGFiZWwoIlswLTldKzpbMC05XSsiKTsKICAgIGNvbnN0IHRyaWdnZXIgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+IC9hc3BlY3QgcmF0aW98dOG7tyBs4buHfHThu4kgbOG7h3xjcm9wXyg/OmxhbmRzY2FwZXxwb3J0cmFpdCkvaXUudGVzdChsYWJlbHMobm9kZSkpCiAgICAgIHx8IChub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1oYXNwb3B1cCIpICYmIGhhc1JhdGlvLnRlc3QobGFiZWxzKG5vZGUpKSkpOwogICAgY29uc3Qgb3B0aW9ucyA9ICgpID0+IGNvbnRyb2xzKCkuZmlsdGVyKG5vZGUgPT4gIXRyaWdnZXIuaW5jbHVkZXMobm9kZSkgJiYgZXhhY3QudGVzdChsYWJlbHMobm9kZSkpKTsKICAgIGNvbnN0IG1vZGVsU2V0dGluZ3MgPSBjb250cm9scygpLmZpbHRlcihub2RlID0+IC9OYW5vIEJhbmFuYXxJbWFnZW4vaXUudGVzdChsYWJlbHMobm9kZSkpICYmIC9beMOXXVsxLTRdXGIvaXUudGVzdChsYWJlbHMobm9kZSkpKTsKICAgIGNvbnN0IHNldHRpbmdzID0gbW9kZWxTZXR0aW5ncy5sZW5ndGggPT09IDEgPyBtb2RlbFNldHRpbmdzWzBdIDogY29udHJvbHMoKS5maW5kKG5vZGUgPT4gL14oxJBp4buBdSBraeG7h24ga8OtY2ggaG/huqF0IGPDoGkgxJHhurd0fFNldHRpbmdzIHRyaWdnZXIpJC9pdS50ZXN0KGNhcHRpb24obm9kZSkpKTsKICAgIGxldCBvcGVuZWQgPSBmYWxzZTsKICAgIGlmICghb3B0aW9ucygpLmxlbmd0aCAmJiBzZXR0aW5ncykgewogICAgICBzZXR0aW5ncy5jbGljaygpOwogICAgICBvcGVuZWQgPSB0cnVlOwogICAgICBjb25zdCBpbWFnZSA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gY29udHJvbHMoKS5maW5kKG5vZGUgPT4gbm9kZS5nZXRBdHRyaWJ1dGUoInJvbGUiKSA9PT0gInJhZGlvIiAmJiAvXihJbWFnZXxIw6xuaCDhuqNuaHzhuqJuaCkkL2l1LnRlc3QoY2FwdGlvbihub2RlKSkpLCAzKTsKICAgICAgaWYgKGltYWdlICYmICFzZWxlY3RlZChpbWFnZSkpIGltYWdlLmNsaWNrKCk7CiAgICB9CiAgICBpZiAoIW9wdGlvbnMoKS5sZW5ndGgpIGF3YWl0IHdhaXRGb3IoKCkgPT4gb3B0aW9ucygpLmxlbmd0aCwgMyk7CiAgICBpZiAoIW9wdGlvbnMoKS5sZW5ndGggJiYgdHJpZ2dlci5sZW5ndGggPT09IDEpIHRyaWdnZXJbMF0uY2xpY2soKTsKICAgIGNvbnN0IG9wdGlvbiA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICBpZiAodHJpZ2dlci5sZW5ndGggPT09IDEgJiYgZXhhY3QudGVzdChsYWJlbHModHJpZ2dlclswXSkpICYmICFvcHRpb25zKCkubGVuZ3RoKSByZXR1cm4gdHJpZ2dlclswXTsKICAgICAgY29uc3QgbWF0Y2hlcyA9IG9wdGlvbnMoKTsKICAgICAgcmV0dXJuIG1hdGNoZXMubGVuZ3RoID09PSAxID8gbWF0Y2hlc1swXSA6IG51bGw7CiAgICB9LCA1KTsKICAgIGlmICghb3B0aW9uKSB0aHJvdyBFcnJvcigiS2jDtG5nIHTDrG0gdGjhuqV5IHThu7cgbOG7hyAiICsgcmF0aW8gKyAiIHRyb25nIEdvb2dsZSBGbG93LiIpOwogICAgaWYgKCF0cmlnZ2VyLmluY2x1ZGVzKG9wdGlvbikgJiYgIXNlbGVjdGVkKG9wdGlvbikpIG9wdGlvbi5jbGljaygpOwogICAgY29uc3Qgc2luZ2xlID0gY29udHJvbHMoKS5maWx0ZXIobm9kZSA9PiAvXlt4w5ddXHMqMSQvaXUudGVzdChjYXB0aW9uKG5vZGUpKSk7CiAgICBpZiAoc2luZ2xlLmxlbmd0aCA9PT0gMSAmJiAhc2VsZWN0ZWQoc2luZ2xlWzBdKSkgc2luZ2xlWzBdLmNsaWNrKCk7CiAgICBpZiAob3BlbmVkICYmIG9wdGlvbnMoKS5sZW5ndGggJiYgc2V0dGluZ3MuaXNDb25uZWN0ZWQpIHNldHRpbmdzLmNsaWNrKCk7CiAgfQogIGFzeW5jIGZ1bmN0aW9uIHJ1bigpIHsKICAgIGxldCBhcHBsaWVkUmF0aW87CiAgICBjb25zdCBiYXRjaCA9IGF3YWl0IHNlbmQoe29wOiJiYXRjaCJ9KTsKICAgIGlmIChiYXRjaC5kb25lKSByZXR1cm47CiAgICBjb25zdCBxdWV1ZWQgPSBbXTsKICAgIGNvbnN0IHJlc3VsdHMgPSBbXTsKICAgIGNvbnN0IGNhcmRzID0gKCkgPT4gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJncm91cCJdW2FyaWEtbGFiZWxdJyldOwogICAgY29uc3Qgbm9ybWFsaXplID0gdGV4dCA9PiAodGV4dCB8fCAiIikucmVwbGFjZSgvXHMrL2d1LCAiICIpLnRyaW0oKTsKICAgIGZvciAoY29uc3QgW29mZnNldCwgcHJvbXB0XSBvZiBiYXRjaC5wcm9tcHRzLmVudHJpZXMoKSkgewogICAgICBjb25zdCBqb2IgPSB7am9iOmJhdGNoLmpvYiwgaW5kZXg6YmF0Y2guaW5kZXggKyBvZmZzZXQsIHByb21wdH07CiAgICAgIGNvbnN0IGFnZW50ID0gWy4uLmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tyb2xlPSJjaGVja2JveCJdLGlucHV0W3R5cGU9ImNoZWNrYm94Il0nKV0uZmluZChub2RlID0+IHZpc2libGUobm9kZSkKICAgICAgICAmJiAvXihUw6FjIG5ow6JufEFnZW50KSQvaXUudGVzdCgobm9kZS5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSB8fCBub2RlLnRleHRDb250ZW50IHx8ICIiKS50cmltKCkpCiAgICAgICAgJiYgKG5vZGUuY2hlY2tlZCB8fCBub2RlLmdldEF0dHJpYnV0ZSgiYXJpYS1jaGVja2VkIikgPT09ICJ0cnVlIikpOwogICAgICBpZiAoYWdlbnQpIHsgYWdlbnQuY2xpY2soKTsgYXdhaXQgc2xlZXAoMzAwKTsgfQogICAgICBsZXQgZmllbGQgPSBhd2FpdCB3YWl0Rm9yKHByb21wdEZpZWxkLCAyNSk7CiAgICAgIGlmICghZmllbGQpIHRocm93IEVycm9yKCJLaMO0bmcgeMOhYyDEkeG7i25oIMSRxrDhu6NjIMO0IHByb21wdCBkdXkgbmjhuqV0LiBN4bufIHRyYW5nIHThuqFvIOG6o25oIHRyb25nIGThu7Egw6FuIEZsb3cuIik7CiAgICAgIGNvbnN0IHJhdGlvID0gL15U4bqhbyBt4buZdCDhuqNuaCBtaW5oIGjhu41hIHThu7cgbOG7hyAoXGQrOlxkKykgL3UuZXhlYyhqb2IucHJvbXB0KT8uWzFdOwogICAgICBpZiAocmF0aW8gJiYgcmF0aW8gIT09IGFwcGxpZWRSYXRpbykgewogICAgICAgIGF3YWl0IHNlbGVjdFJhdGlvKHJhdGlvKTsKICAgICAgICBhcHBsaWVkUmF0aW8gPSByYXRpbzsKICAgICAgICBmaWVsZCA9IGF3YWl0IHdhaXRGb3IocHJvbXB0RmllbGQsIDEwKTsKICAgICAgICBpZiAoIWZpZWxkKSB0aHJvdyBFcnJvcigiS2jDtG5nIHRo4bqleSDDtCBwcm9tcHQgc2F1IGtoaSBjaOG7jW4gdOG7tyBs4buHLiIpOwogICAgICB9CiAgICAgIGF3YWl0IHNlbmQoe29wOiJhY3RpdmF0ZSJ9KTsKICAgICAgaWYgKCFhd2FpdCB3YWl0Rm9yKCgpID0+IGRvY3VtZW50Lmhhc0ZvY3VzKCkgJiYgIWRvY3VtZW50LmhpZGRlbiwgNikpCiAgICAgICAgdGhyb3cgRXJyb3IoIkNocm9tZSBjaMawYSDEkcawYSB0YWIgRmxvdyBsw6puIHRyxrDhu5tjIMSR4buDIGfhu61pIHByb21wdC4iKTsKICAgICAgZmllbGQuZm9jdXMoKTsKICAgICAgaWYgKGZpZWxkIGluc3RhbmNlb2YgSFRNTFRleHRBcmVhRWxlbWVudCkgewogICAgICAgIE9iamVjdC5nZXRPd25Qcm9wZXJ0eURlc2NyaXB0b3IoSFRNTFRleHRBcmVhRWxlbWVudC5wcm90b3R5cGUsInZhbHVlIikuc2V0LmNhbGwoZmllbGQsam9iLnByb21wdCk7CiAgICAgICAgZmllbGQuZGlzcGF0Y2hFdmVudChuZXcgSW5wdXRFdmVudCgiaW5wdXQiLHtidWJibGVzOnRydWUsZGF0YTpqb2IucHJvbXB0LGlucHV0VHlwZToiaW5zZXJ0VGV4dCJ9KSk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3Qgc2VsZWN0aW9uID0gd2luZG93LmdldFNlbGVjdGlvbigpOwogICAgICAgIGNvbnN0IHJhbmdlID0gZG9jdW1lbnQuY3JlYXRlUmFuZ2UoKTsKICAgICAgICByYW5nZS5zZWxlY3ROb2RlQ29udGVudHMoZmllbGQpOwogICAgICAgIHNlbGVjdGlvbi5yZW1vdmVBbGxSYW5nZXMoKTsKICAgICAgICBzZWxlY3Rpb24uYWRkUmFuZ2UocmFuZ2UpOwogICAgICAgIGlmICghZG9jdW1lbnQuZXhlY0NvbW1hbmQoImluc2VydFRleHQiLGZhbHNlLGpvYi5wcm9tcHQpKSB0aHJvdyBFcnJvcigiS2jDtG5nIMSRaeG7gW4gxJHGsOG7o2MgcHJvbXB0IHRyb25nIEZsb3cuIik7CiAgICAgIH0KICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgICAgY29uc3QgYnV0dG9ucyA9IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSIpXS5maWx0ZXIoYj0+dmlzaWJsZShiKSYmIWIuZGlzYWJsZWQmJmIuZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIpOwogICAgICBjb25zdCBzdGFydCA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiAvXihC4bqvdCDEkeG6p3UgdOG6oW98U3RhcnQgZ2VuZXJhdGluZ3xTdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKSk7CiAgICAgIGxldCBnZW5lcmF0ZSA9IHN0YXJ0Lmxlbmd0aCA9PT0gMSA/IHN0YXJ0IDogYnV0dG9ucy5maWx0ZXIoYj0+L14oR2VuZXJhdGUgSW1hZ2V8Q3JlYXRlIEltYWdlfFThuqFvIGjDrG5oIOG6o25ofFThuqFvIOG6o25oKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCAmJiBtYXRjaGluZygvTmFubyBCYW5hbmF8SW1hZ2VuL2kpLmxlbmd0aCA9PT0gMSkKICAgICAgICBnZW5lcmF0ZSA9IGJ1dHRvbnMuZmlsdGVyKGI9Pi9eKEdlbmVyYXRlfFThuqFvKSQvaS50ZXN0KGNhcHRpb24oYikpKTsKICAgICAgaWYgKGdlbmVyYXRlLmxlbmd0aCA9PT0gMCkgewogICAgICAgIGNvbnN0IGVkaXRvciA9IGZpZWxkLmNsb3Nlc3QoImZvcm0iKSB8fCBmaWVsZC5wYXJlbnRFbGVtZW50OwogICAgICAgIGdlbmVyYXRlID0gYnV0dG9ucy5maWx0ZXIoYnV0dG9uID0+IC9eKGFycm93X2ZvcndhcmR8c2VuZHxn4butaXxi4bqvdCDEkeG6p3UgdOG6oW98c3RhcnQgZ2VuZXJhdGluZ3xzdGFydCBjcmVhdGluZykkL2l1LnRlc3QoY2FwdGlvbihidXR0b24pKQogICAgICAgICAgJiYgKGVkaXRvcj8uY29udGFpbnMoYnV0dG9uKSB8fCBmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5ib3R0b20gPD0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpLmJvdHRvbQogICAgICAgICAgICAmJiBNYXRoLmFicyhmaWVsZC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCAtIGJ1dHRvbi5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKS5yaWdodCkgPCAxMDApKTsKICAgICAgfQogICAgICBpZiAoZ2VuZXJhdGUubGVuZ3RoICE9PSAxKSB0aHJvdyBFcnJvcigiS2jDtG5nIG5o4bqtbiBkaeG7h24gxJHGsOG7o2MgbsO6dCB04bqhbyDhuqNuaC4gS2nhu4NtIHRyYSBjaOG6vyDEkeG7mSDhuqJuaCwgbW9kZWwgdsOgIGjhuqFuIG3hu6ljIEZsb3cuIik7CiAgICAgIGNvbnN0IHByaW9yID0gbmV3IFNldChjYXJkcygpKTsKICAgICAgY29uc3QgZWRpdG9yQm94ID0gZmllbGQuZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICAgIGNvbnN0IG5lYXJieUFycm93cyA9IGJ1dHRvbnMuZmlsdGVyKGJ1dHRvbiA9PiB7CiAgICAgICAgY29uc3QgYm94ID0gYnV0dG9uLmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICAgIHJldHVybiBib3gudG9wID49IGVkaXRvckJveC5ib3R0b20gLSA4ICYmIGJveC50b3AgPD0gZWRpdG9yQm94LmJvdHRvbSArIDEwMAogICAgICAgICAgJiYgTWF0aC5hYnMoYm94LnJpZ2h0IC0gZWRpdG9yQm94LnJpZ2h0KSA8IDEwMAogICAgICAgICAgJiYgKFsuLi5idXR0b24ucXVlcnlTZWxlY3RvckFsbCgibWF0LWljb24sW2RhdGEtaWNvbl0sW2FyaWEtaGlkZGVuPXRydWVdLGltZyIpXS5zb21lKGljb24gPT4KICAgICAgICAgICAgL14oYXJyb3dfZm9yd2FyZHxzZW5kKSQvaS50ZXN0KChpY29uLmdldEF0dHJpYnV0ZSgiZGF0YS1pY29uIikgfHwgaWNvbi5nZXRBdHRyaWJ1dGUoImFsdCIpIHx8IGljb24udGV4dENvbnRlbnQgfHwgIiIpLnRyaW0oKSkpCiAgICAgICAgICAgIHx8IC9eKELhuq90IMSR4bqndSB04bqhb3xTdGFydCBnZW5lcmF0aW5nfFN0YXJ0IGNyZWF0aW5nKSQvaXUudGVzdChjYXB0aW9uKGJ1dHRvbikpKTsKICAgICAgfSk7CiAgICAgIGNvbnN0IHN1Ym1pdCA9IG5lYXJieUFycm93cy5sZW5ndGggPT09IDEgPyBuZWFyYnlBcnJvd3NbMF0gOiBnZW5lcmF0ZVswXTsKICAgICAgc3VibWl0LnNjcm9sbEludG9WaWV3KHtibG9jazoibmVhcmVzdCIsIGlubGluZToibmVhcmVzdCJ9KTsKICAgICAgY29uc3QgYm94ID0gc3VibWl0LmdldEJvdW5kaW5nQ2xpZW50UmVjdCgpOwogICAgICBjb25zdCB4ID0gYm94LmxlZnQgKyBib3gud2lkdGggLyAyOwogICAgICBjb25zdCB5ID0gYm94LnRvcCArIGJveC5oZWlnaHQgLyAyOwogICAgICBpZiAoIXN1Ym1pdC5jb250YWlucyhkb2N1bWVudC5lbGVtZW50RnJvbVBvaW50KHgseSkpKSB0aHJvdyBFcnJvcigiTsO6dCB04bqhbyDhuqNuaCDEkWFuZyBi4buLIGNoZS4iKTsKICAgICAgYXdhaXQgc2VuZCh7b3A6InN1Ym1pdCIsIGpvYjpqb2Iuam9iLCB4LCB5fSk7CiAgICAgIGNvbnN0IGZpbmRDYXJkID0gKCkgPT4gewogICAgICAgIGNvbnN0IG1hdGNoZXMgPSBjYXJkcygpLmZpbHRlcihjYXJkID0+ICFwcmlvci5oYXMoY2FyZCkgJiYKICAgICAgICAgIChub3JtYWxpemUoY2FyZC5nZXRBdHRyaWJ1dGUoImFyaWEtbGFiZWwiKSkgPT09IG5vcm1hbGl6ZShwcm9tcHQpCiAgICAgICAgICAgfHwgbm9ybWFsaXplKGNhcmQudGV4dENvbnRlbnQpLmluY2x1ZGVzKG5vcm1hbGl6ZShwcm9tcHQpKSkpOwogICAgICAgIHJldHVybiBtYXRjaGVzLmxlbmd0aCA9PT0gMSA/IG1hdGNoZXNbMF0gOiBudWxsOwogICAgICB9OwogICAgICBjb25zdCBjYXJkID0gYXdhaXQgd2FpdEZvcihmaW5kQ2FyZCwgMjApOwogICAgICBpZiAoIWNhcmQpIHRocm93IEVycm9yKCJGbG93IGNoxrBhIHjDoWMgbmjhuq1uIMSRw6Mgbmjhuq1uIHByb21wdCBj4bqjbmggIiArIChqb2IuaW5kZXggKyAxKSArICIuIik7CiAgICAgIHF1ZXVlZC5wdXNoKHtqb2IsIGNhcmQsIGZpbmRDYXJkfSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnF1ZXVlZC5sZW5ndGgsIHN0YWdlOiJzdWJtaXR0ZWQifSk7CiAgICB9CiAgICBjb25zdCBjbGFpbWVkID0gbmV3IFNldCgpOwogICAgZm9yIChjb25zdCB7am9iLCBjYXJkLCBmaW5kQ2FyZH0gb2YgcXVldWVkKSB7CiAgICAgIGNvbnN0IHJlc3VsdCA9IGF3YWl0IHdhaXRGb3IoKCkgPT4gewogICAgICAgIGNvbnN0IGN1cnJlbnQgPSAoY2FyZC5pc0Nvbm5lY3RlZCAmJiByZXN1bHRJbWFnZXMoY2FyZCkubGVuZ3RoID8gY2FyZCA6IGZpbmRDYXJkKCkpIHx8IChjYXJkLmlzQ29ubmVjdGVkID8gY2FyZCA6IG51bGwpOwogICAgICAgIGlmICghY3VycmVudCB8fCBjbGFpbWVkLmhhcyhjdXJyZW50KSkgcmV0dXJuIG51bGw7CiAgICAgICAgY29uc3QgdXJscyA9IFsuLi5uZXcgU2V0KHJlc3VsdEltYWdlcyhjdXJyZW50KS5tYXAoaW1hZ2UgPT4gaW1hZ2UuY3VycmVudFNyYyB8fCBpbWFnZS5zcmMpKV07CiAgICAgICAgaWYgKHVybHMubGVuZ3RoICE9PSAxKSByZXR1cm4gbnVsbDsKICAgICAgICBjbGFpbWVkLmFkZChjdXJyZW50KTsKICAgICAgICByZXR1cm4gdXJsc1swXTsKICAgICAgfSwgMTc1KTsKICAgICAgaWYgKCFyZXN1bHQpIHRocm93IEVycm9yKCJLaMO0bmcgdMOsbSB0aOG6pXkg4bqjbmgga+G6v3QgcXXhuqMgY+G7p2EgY+G6o25oICIgKyAoam9iLmluZGV4ICsgMSkgKyAiLiIpOwogICAgICByZXN1bHRzLnB1c2goe2pvYiwgcmVzdWx0fSk7CiAgICAgIGF3YWl0IHNlbmQoe29wOiJwcm9ncmVzcyIsIGNvdW50OnJlc3VsdHMubGVuZ3RofSk7CiAgICB9CiAgICBmb3IgKGNvbnN0IHtqb2IsIHJlc3VsdH0gb2YgcmVzdWx0cykgewogICAgICBsZXQgZGF0YTsKICAgICAgdHJ5IHsKICAgICAgICBjb25zdCByZXNwb25zZSA9IGF3YWl0IGZldGNoKHJlc3VsdCk7CiAgICAgICAgaWYgKCFyZXNwb25zZS5vaykgdGhyb3cgRXJyb3IoIktow7RuZyB04bqjaSDEkcaw4bujYyDhuqNuaCB0csOqbiB0YWIuIik7CiAgICAgICAgY29uc3QgYmxvYiA9IGF3YWl0IHJlc3BvbnNlLmJsb2IoKTsKICAgICAgICBpZiAoIWJsb2IudHlwZS5zdGFydHNXaXRoKCJpbWFnZS8iKSB8fCBibG9iLnNpemUgPiAyNCoxMDI0KjEwMjQpIHRocm93IEVycm9yKCLhuqJuaCBraMO0bmcgaOG7o3AgbOG7hy4iKTsKICAgICAgICBkYXRhID0gYXdhaXQgbmV3IFByb21pc2UoKHJlc29sdmUscmVqZWN0KT0+e2NvbnN0IHJlYWRlcj1uZXcgRmlsZVJlYWRlcigpO3JlYWRlci5vbmxvYWQ9KCk9PnJlc29sdmUocmVhZGVyLnJlc3VsdCk7cmVhZGVyLm9uZXJyb3I9cmVqZWN0O3JlYWRlci5yZWFkQXNEYXRhVVJMKGJsb2IpO30pOwogICAgICB9IGNhdGNoIChlcnJvcikgeyBpZiAocmVzdWx0LnN0YXJ0c1dpdGgoImJsb2I6IikpIHRocm93IGVycm9yOyB9CiAgICAgIGF3YWl0IHNlbmQoe29wOiJpbWFnZSIsam9iOmpvYi5qb2IsaW5kZXg6am9iLmluZGV4LHVybDpyZXN1bHQsZGF0YX0pOwogICAgfQogIH0KICBhc3luYyBmdW5jdGlvbiB3YWl0Rm9yKGZpbmQsIHNlY29uZHMpIHsKICAgIGZvciAobGV0IGk9MDtpPHNlY29uZHMqMjtpKyspIHsKICAgICAgY29uc3QgdmFsdWU9ZmluZCgpOwogICAgICBpZiAodmFsdWUpIHJldHVybiB2YWx1ZTsKICAgICAgYXdhaXQgc2xlZXAoNTAwKTsKICAgIH0KICAgIHJldHVybiBudWxsOwogIH0KICBjb25zdCBwcm9qZWN0T3BlbiA9ICgpID0+IC9cL3Byb2plY3RzP1wvW14vXSsvLnRlc3QobG9jYXRpb24ucGF0aG5hbWUpOwogIGNvbnN0IG1hdGNoaW5nID0gKHBhdHRlcm4pID0+IFsuLi5kb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCJidXR0b24sW3JvbGU9YnV0dG9uXSxbcm9sZT10YWJdLGEiKV0KICAgIC5maWx0ZXIoaXRlbT0+dmlzaWJsZShpdGVtKSYmIWl0ZW0uZGlzYWJsZWQmJml0ZW0uZ2V0QXR0cmlidXRlKCJhcmlhLWRpc2FibGVkIikhPT0idHJ1ZSIKICAgICAgJiYgKHBhdHRlcm4udGVzdChjYXB0aW9uKGl0ZW0pKSB8fCBwYXR0ZXJuLnRlc3QoKGl0ZW0uaW5uZXJUZXh0IHx8IGl0ZW0udGV4dENvbnRlbnQgfHwgIiIpLnJlcGxhY2UoL1xzKy9ndSwgIiAiKS50cmltKCkpKSk7CiAgYXN5bmMgZnVuY3Rpb24gY29ubmVjdCgpIHsKICAgIGNvbnN0IHN0YXRlID0gYXdhaXQgc2VuZChwYWlyaW5nID8ge29wOiJwYWlyIix0b2tlbjpwYWlyaW5nfSA6IHtvcDoicmVzdW1lIn0pOwogICAgaWYgKHN0YXRlLmRvbmUpIHJldHVybjsKICAgIGlmIChwYWlyaW5nKSBoaXN0b3J5LnJlcGxhY2VTdGF0ZShoaXN0b3J5LnN0YXRlLCAiIiwgbG9jYXRpb24ucGF0aG5hbWUgKyBsb2NhdGlvbi5zZWFyY2gpOyAKICAgIGlmIChkb2N1bWVudC5yZWFkeVN0YXRlID09PSAibG9hZGluZyIpIGF3YWl0IG5ldyBQcm9taXNlKHJlc29sdmU9PmRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoIkRPTUNvbnRlbnRMb2FkZWQiLHJlc29sdmUse29uY2U6dHJ1ZX0pKTsKICAgIGlmICghcHJvamVjdE9wZW4oKSkgewogICAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJGbG93IHbhuqtuIOG7nyB0cmFuZyBjaOG7pyBzYXUga2hpIHThuqFvIGThu7Egw6FuLiBLaeG7g20gdHJhIMSRxINuZyBuaOG6rXAgcuG7k2kgY2jhuqF5IGzhuqFpLiIpOwogICAgICBjb25zdCBjcmVhdGUgPSBhd2FpdCB3YWl0Rm9yKCgpID0+IHsKICAgICAgICBjb25zdCBjaG9pY2VzID0gbWF0Y2hpbmcoL14oPzooPzphZGQoPzpfY2lyY2xlfF9ib3gpP3xwbHVzfFwrKVxzKik/KD86TmV3IHByb2plY3R8Q3JlYXRlIHByb2plY3R8Q3JlYXRlIG5ldyBwcm9qZWN0fEThu7Egw6FuIG3hu5tpfFThuqFvIGThu7Egw6FufFThuqFvIGThu7Egw6FuIG3hu5tpKSQvaXUpOwogICAgICAgIGNvbnN0IGNvbnRyb2xzID0gY2hvaWNlcy5maWx0ZXIoaXRlbSA9PiAhY2hvaWNlcy5zb21lKG90aGVyID0+IG90aGVyICE9PSBpdGVtICYmIGl0ZW0uY29udGFpbnMob3RoZXIpKSk7CiAgICAgICAgcmV0dXJuIGNvbnRyb2xzWzBdIHx8IG51bGw7CiAgICAgIH0sMzUpOwogICAgICBpZiAoIWNyZWF0ZSkgdGhyb3cgRXJyb3IoIktow7RuZyB0w6xtIHRo4bqleSBuw7p0IHThuqFvIGThu7Egw6FuIEZsb3cuIEtp4buDbSB0cmEgQ2hyb21lIMSRw6MgxJHEg25nIG5o4bqtcCB2w6AgeGVtIHRyYW5nIEZsb3cgY8OzIHRow7RuZyBiw6FvIGfDrC4iKTsKICAgICAgY3JlYXRlLmNsaWNrKCk7CiAgICAgIGlmICghYXdhaXQgd2FpdEZvcihwcm9qZWN0T3Blbiw0NSkpIHRocm93IEVycm9yKCJGbG93IGNoxrBhIG3hu58gZOG7sSDDoW4gbeG7m2kgc2F1IGtoaSBi4bqlbSB04bqhby4iKTsKICAgIH0KICAgIGlmICghYXdhaXQgd2FpdEZvcigoKT0+ZG9jdW1lbnQucXVlcnlTZWxlY3RvcigndGV4dGFyZWEsW2NvbnRlbnRlZGl0YWJsZT0idHJ1ZSJdJyksNDUpKQogICAgICB0aHJvdyBFcnJvcigiROG7sSDDoW4gbeG7m2kgY2jGsGEgaGnhu4duIMO0IHByb21wdC4iKTsKICAgIAogICAgYXdhaXQgcnVuKCk7CiAgfQogIGNvbm5lY3QoKS5jYXRjaChhc3luYyBlcnJvciA9PiB7CiAgICB0cnkge2F3YWl0IHNlbmQoe29wOiJlcnJvciIsbWVzc2FnZTplcnJvci5tZXNzYWdlfSk7fSBjYXRjaCB7IGNvbnNvbGUud2FybigiVuG6vSBUYXk6IGdow6lwIG7hu5FpIGhv4bq3YyBn4butaSB0cuG6oW5nIHRow6FpIHRo4bqldCBi4bqhaS4iLCBlcnJvci5tZXNzYWdlKTsgfQogIH0pLmZpbmFsbHkoKCk9Pnt3aW5kb3cuX192ZVRheVJ1bm5pbmc9ZmFsc2U7fSk7Cn0pKCk7",
    "worker.js": "Y29uc3QgYmFzZSA9ICJodHRwOi8vMTI3LjAuMC4xOjc4NzciOwpmdW5jdGlvbiBmbG93VGFiKHVybCkgeyByZXR1cm4gL15odHRwczpcL1wvKGZsb3dcLmdvb2dsZVwuY29tfGxhYnNcLmdvb2dsZSlcLy8udGVzdCh1cmwgfHwgIiIpOyB9CmFzeW5jIGZ1bmN0aW9uIGFwaSh0b2tlbiwgcGF0aCwgYm9keSkgewogIGNvbnN0IHIgPSBhd2FpdCBmZXRjaChiYXNlICsgcGF0aCwgewogICAgaGVhZGVyczogeyJYLVZlVGF5LVRva2VuIjp0b2tlbiwgLi4uKGJvZHkgPyB7IkNvbnRlbnQtVHlwZSI6Ym9keS50eXBlIHx8ICJhcHBsaWNhdGlvbi9qc29uIn0gOiB7fSl9LAogICAgbWV0aG9kOiBib2R5ID8gIlBPU1QiIDogIkdFVCIsIGJvZHkKICB9KTsKICBpZiAoIXIub2spIHRocm93IEVycm9yKChhd2FpdCByLnRleHQoKSkuc2xpY2UoMCwyNTApKTsKICByZXR1cm4gci5qc29uKCk7Cn0KY2hyb21lLnJ1bnRpbWUub25NZXNzYWdlLmFkZExpc3RlbmVyKChtLHNlbmRlcixyZXBseSkgPT4gewogIChhc3luYygpPT57CiAgICBpZiAoIWZsb3dUYWIoc2VuZGVyLnRhYj8udXJsKSkgdGhyb3cgRXJyb3IoIkNo4buJIG5o4bqtbiB5w6p1IGPhuqd1IHThu6sgdGFiIEZsb3cuIik7CiAgICBjb25zdCBrZXkgPSAicGFpcmluZzoiICsgc2VuZGVyLnRhYi5pZDsKICAgIGlmIChtLm9wID09PSAicGFpciIpIHsKICAgICAgaWYgKCEvXlthLWYwLTldezMyfSQvLnRlc3QobS50b2tlbiB8fCAiIikpIHRocm93IEVycm9yKCJNw6Mga+G6v3QgbuG7kWkga2jDtG5nIGjhu6NwIGzhu4cuIik7CiAgICAgIGNvbnN0IGpvYiA9IGF3YWl0IGFwaShtLnRva2VuLCAiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lKSB0aHJvdyBFcnJvcigiVuG6vSBUYXkgY2jGsGEgY8OzIGPhuqNuaCBjaOG7nS4iKTsKICAgICAgYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5zZXQoe1trZXldOm0udG9rZW59KTsKICAgICAgcmV0dXJuIGpvYjsKICAgIH0KICAgIGNvbnN0IHNhdmVkID0gYXdhaXQgY2hyb21lLnN0b3JhZ2Uuc2Vzc2lvbi5nZXQoa2V5KTsKICAgIGNvbnN0IHBhaXJpbmcgPSBzYXZlZFtrZXldOwogICAgaWYgKG0ub3AgPT09ICJyZXN1bWUiKSByZXR1cm4gcGFpcmluZyA/IGFwaShwYWlyaW5nLCIvbmV4dCIpIDoge2RvbmU6dHJ1ZX07CiAgICBpZiAoIXBhaXJpbmcpIHRocm93IEVycm9yKCJUYWIgY2jGsGEgxJHGsOG7o2MgVuG6vSBUYXkga+G6v3QgbuG7kWkuIik7CiAgICBpZiAobS5vcCA9PT0gInN1Ym1pdCIpIHsKICAgICAgY29uc3QgdGFiID0gYXdhaXQgY2hyb21lLnRhYnMuZ2V0KHNlbmRlci50YWIuaWQpOwogICAgICBpZiAoIXRhYi5hY3RpdmUgfHwgc2VuZGVyLmZyYW1lSWQgIT09IDAgfHwgbmV3IFVSTCh0YWIudXJsKS5vcmlnaW4gIT09ICJodHRwczovL2Zsb3cuZ29vZ2xlLmNvbSIKICAgICAgICB8fCAhL15cL3Byb2plY3RcL1teL10rXC8/JC8udGVzdChuZXcgVVJMKHRhYi51cmwpLnBhdGhuYW1lKQogICAgICAgIHx8ICFbbS54LG0ueV0uZXZlcnkoTnVtYmVyLmlzRmluaXRlKSB8fCBtLnggPCAwIHx8IG0ueSA8IDAgfHwgbS54ID4gMTAwMDAgfHwgbS55ID4gMTAwMDApCiAgICAgICAgdGhyb3cgRXJyb3IoIlRhYiBk4buxIMOhbiBGbG93IGtow7RuZyBjw7JuIOG7nyB0cuG6oW5nIHRow6FpIHPhurVuIHPDoG5nLiIpOwogICAgICBjb25zdCBqb2IgPSBhd2FpdCBhcGkocGFpcmluZywiL25leHQiKTsKICAgICAgaWYgKGpvYi5kb25lIHx8IGpvYi5qb2IgIT09IG0uam9iKSB0aHJvdyBFcnJvcigiVMOhYyB24bulIFbhur0gVGF5IMSRw6MgxJHhu5VpLiIpOwogICAgICBjb25zdCB0YXJnZXQgPSB7dGFiSWQ6dGFiLmlkfTsKICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmF0dGFjaCh0YXJnZXQsIjEuMyIpOwogICAgICB0cnkgewogICAgICAgIGNvbnN0IGlucHV0ID0gKHR5cGUsYnV0dG9ucykgPT4gY2hyb21lLmRlYnVnZ2VyLnNlbmRDb21tYW5kKHRhcmdldCwiSW5wdXQuZGlzcGF0Y2hNb3VzZUV2ZW50IiwKICAgICAgICAgIHt0eXBlLHg6bS54LHk6bS55LGJ1dHRvbjoibGVmdCIsYnV0dG9ucyxjbGlja0NvdW50OjF9KTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VQcmVzc2VkIiwxKTsKICAgICAgICBhd2FpdCBpbnB1dCgibW91c2VSZWxlYXNlZCIsMCk7CiAgICAgIH0gZmluYWxseSB7CiAgICAgICAgYXdhaXQgY2hyb21lLmRlYnVnZ2VyLmRldGFjaCh0YXJnZXQpOwogICAgICB9CiAgICAgIHJldHVybiB7Y2xpY2tlZDp0cnVlfTsKICAgIH0KICAgIGlmIChtLm9wID09PSAiYWN0aXZhdGUiKSB7CiAgICAgIGF3YWl0IGNocm9tZS50YWJzLnVwZGF0ZShzZW5kZXIudGFiLmlkLCB7YWN0aXZlOnRydWV9KTsKICAgICAgYXdhaXQgY2hyb21lLndpbmRvd3MudXBkYXRlKHNlbmRlci50YWIud2luZG93SWQsIHtmb2N1c2VkOnRydWV9KTsKICAgICAgcmV0dXJuIHthY3RpdmU6dHJ1ZX07CiAgICB9CiAgICBpZiAobS5vcCA9PT0gIm5leHQiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9uZXh0Iik7CiAgICBpZiAobS5vcCA9PT0gImJhdGNoIikgcmV0dXJuIGFwaShwYWlyaW5nLCIvYmF0Y2giKTsKICAgIGlmIChtLm9wID09PSAicHJvZ3Jlc3MiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9wcm9ncmVzcyIsSlNPTi5zdHJpbmdpZnkoe2NvdW50Om0uY291bnQsc3RhZ2U6bS5zdGFnZX0pKTsKICAgIGlmIChtLm9wID09PSAiZXJyb3IiKSByZXR1cm4gYXBpKHBhaXJpbmcsIi9lcnJvciIsSlNPTi5zdHJpbmdpZnkoe21lc3NhZ2U6bS5tZXNzYWdlfSkpOwogICAgaWYgKG0ub3AgPT09ICJpbWFnZSIgJiYgL15bYS1mMC05XXszMn0kLy50ZXN0KG0uam9iKSAmJiBOdW1iZXIuaXNJbnRlZ2VyKG0uaW5kZXgpKSB7CiAgICAgIGxldCBpbWFnZTsKICAgICAgaWYgKG0uZGF0YSAmJiAvXmRhdGE6aW1hZ2VcLyhwbmd8d2VicHxqcGVnKTtiYXNlNjQsLy50ZXN0KG0uZGF0YSkpIHsKICAgICAgICBpbWFnZSA9IGF3YWl0IChhd2FpdCBmZXRjaChtLmRhdGEpKS5ibG9iKCk7CiAgICAgIH0gZWxzZSB7CiAgICAgICAgY29uc3QgdSA9IG5ldyBVUkwobS51cmwpOwogICAgICAgIGlmICh1LnByb3RvY29sICE9PSAiaHR0cHM6IiB8fCAhLyhefFwuKWdvb2dsZXVzZXJjb250ZW50XC5jb20kLy50ZXN0KHUuaG9zdG5hbWUpKSB0aHJvdyBFcnJvcigiS2jDtG5nIHjDoWMgxJHhu4tuaCDEkcaw4bujYyDEkeG7i2EgY2jhu4kg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgY29uc3QgciA9IGF3YWl0IGZldGNoKHUuaHJlZix7Y3JlZGVudGlhbHM6Im9taXQiLHJlZGlyZWN0OiJlcnJvciJ9KTsKICAgICAgICBpZiAoIXIub2spIHRocm93IEVycm9yKCJLaMO0bmcgdOG6o2kgxJHGsOG7o2Mg4bqjbmgga+G6v3QgcXXhuqMuIik7CiAgICAgICAgaW1hZ2UgPSBhd2FpdCByLmJsb2IoKTsKICAgICAgfQogICAgICBpZiAoaW1hZ2Uuc2l6ZSA+IDI0KjEwMjQqMTAyNCB8fCAhaW1hZ2UudHlwZS5zdGFydHNXaXRoKCJpbWFnZS8iKSkgdGhyb3cgRXJyb3IoIuG6om5oIGvhur90IHF14bqjIGtow7RuZyBo4bujcCBs4buHLiIpOwogICAgICByZXR1cm4gYXBpKHBhaXJpbmcsIi9pbWFnZS8iK20uam9iKyIvIittLmluZGV4LGltYWdlKTsKICAgIH0KICAgIHRocm93IEVycm9yKCJZw6p1IGPhuqd1IGtow7RuZyBo4bujcCBs4buHLiIpOwogIH0pKCkudGhlbihyZXN1bHQ9PnJlcGx5KHtvazp0cnVlLHJlc3VsdH0pLGVycm9yPT5yZXBseSh7b2s6ZmFsc2UsZXJyb3I6ZXJyb3IubWVzc2FnZX0pKTsKICByZXR1cm4gdHJ1ZTsKfSk7CmNocm9tZS50YWJzLm9uUmVtb3ZlZC5hZGRMaXN0ZW5lcih0YWJJZCA9PiBjaHJvbWUuc3RvcmFnZS5zZXNzaW9uLnJlbW92ZSgicGFpcmluZzoiICsgdGFiSWQpKTsKCmNvbnN0IGV4dGVuc2lvbkNoZWNrID0gInZldGF5LWNoZWNrLWxvY2FsLXVwZGF0ZSI7CmZ1bmN0aW9uIHJlZ2lzdGVyQ2hlY2soKSB7CiAgY2hyb21lLmFsYXJtcy5jcmVhdGUoZXh0ZW5zaW9uQ2hlY2ssIHtwZXJpb2RJbk1pbnV0ZXM6MX0pOwp9CmNocm9tZS5ydW50aW1lLm9uSW5zdGFsbGVkLmFkZExpc3RlbmVyKHJlZ2lzdGVyQ2hlY2spOwpjaHJvbWUucnVudGltZS5vblN0YXJ0dXAuYWRkTGlzdGVuZXIocmVnaXN0ZXJDaGVjayk7CmNocm9tZS5hbGFybXMub25BbGFybS5hZGRMaXN0ZW5lcihhc3luYyBhbGFybSA9PiB7CiAgaWYgKGFsYXJtLm5hbWUgIT09IGV4dGVuc2lvbkNoZWNrKSByZXR1cm47CiAgdHJ5IHsKICAgIGNvbnN0IGN1cnJlbnQgPSBjaHJvbWUucnVudGltZS5nZXRNYW5pZmVzdCgpOwogICAgY29uc3QgcmVzcG9uc2UgPSBhd2FpdCBmZXRjaChjaHJvbWUucnVudGltZS5nZXRVUkwoIm1hbmlmZXN0Lmpzb24iKSArICI/dD0iICsgRGF0ZS5ub3coKSwge2NhY2hlOiJuby1zdG9yZSJ9KTsKICAgIGlmICghcmVzcG9uc2Uub2spIHJldHVybjsKICAgIGNvbnN0IGRpc2sgPSBhd2FpdCByZXNwb25zZS5qc29uKCk7CiAgICBpZiAoIS9eXGQrXC5cZCtcLlxkKyQvLnRlc3QoZGlzay52ZXJzaW9uKSB8fCBkaXNrLm5hbWUgIT09IGN1cnJlbnQubmFtZSB8fAogICAgICAgIGRpc2subWFuaWZlc3RfdmVyc2lvbiAhPT0gY3VycmVudC5tYW5pZmVzdF92ZXJzaW9uKSByZXR1cm47CiAgICBjb25zdCBvbGQgPSBjdXJyZW50LnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IG5leHQgPSBkaXNrLnZlcnNpb24uc3BsaXQoIi4iKS5tYXAoTnVtYmVyKTsKICAgIGNvbnN0IGNoYW5nZWQgPSBuZXh0LmZpbmRJbmRleCgodmFsdWUsaSk9PnZhbHVlIT09b2xkW2ldKTsKICAgIGlmIChjaGFuZ2VkIDwgMCB8fCBuZXh0W2NoYW5nZWRdIDwgb2xkW2NoYW5nZWRdKSByZXR1cm47CiAgICBmb3IoY29uc3Qga2V5IG9mIFsicGVybWlzc2lvbnMiLCJob3N0X3Blcm1pc3Npb25zIiwib3B0aW9uYWxfcGVybWlzc2lvbnMiLCJvcHRpb25hbF9ob3N0X3Blcm1pc3Npb25zIl0pIHsKICAgICAgaWYoSlNPTi5zdHJpbmdpZnkoWy4uLihjdXJyZW50W2tleV18fFtdKV0uc29ydCgpKSE9PUpTT04uc3RyaW5naWZ5KFsuLi4oZGlza1trZXldfHxbXSldLnNvcnQoKSkpIHJldHVybjsKICAgIH0KICAgIGNvbnN0IG1hdGNoZXMgPSB2YWx1ZSA9PiAodmFsdWUuY29udGVudF9zY3JpcHRzIHx8IFtdKS5mbGF0TWFwKHNjcmlwdD0+c2NyaXB0Lm1hdGNoZXMgfHwgW10pLnNvcnQoKTsKICAgIGlmKEpTT04uc3RyaW5naWZ5KG1hdGNoZXMoY3VycmVudCkpIT09SlNPTi5zdHJpbmdpZnkobWF0Y2hlcyhkaXNrKSkpIHJldHVybjsKICAgIGNvbnN0IGlkbGUgPSBhd2FpdCBmZXRjaChiYXNlKyIvZXh0ZW5zaW9uLWlkbGUiLHtjYWNoZToibm8tc3RvcmUifSk7CiAgICBpZihpZGxlLm9rICYmIChhd2FpdCBpZGxlLmpzb24oKSkuaWRsZSA9PT0gdHJ1ZSkgY2hyb21lLnJ1bnRpbWUucmVsb2FkKCk7CiAgfSBjYXRjaCB7fQp9KTsK",
    "manifest.json": "ewogICJtYW5pZmVzdF92ZXJzaW9uIjogMywKICAibmFtZSI6ICJW4bq9IFRheSDCtyBHb29nbGUgRmxvdyIsCiAgInZlcnNpb24iOiAiMS4zLjE0IiwKICAiYWN0aW9uIjogewogICAgImRlZmF1bHRfcG9wdXAiOiAicG9wdXAuaHRtbCIKICB9LAogICJiYWNrZ3JvdW5kIjogewogICAgInNlcnZpY2Vfd29ya2VyIjogIndvcmtlci5qcyIKICB9LAogICJwZXJtaXNzaW9ucyI6IFsKICAgICJzdG9yYWdlIiwKICAgICJhbGFybXMiLAogICAgImRlYnVnZ2VyIgogIF0sCiAgImhvc3RfcGVybWlzc2lvbnMiOiBbCiAgICAiaHR0cDovLzEyNy4wLjAuMTo3ODc3LyoiLAogICAgImh0dHBzOi8vKi5nb29nbGV1c2VyY29udGVudC5jb20vKiIsCiAgICAiaHR0cHM6Ly9mbG93Lmdvb2dsZS5jb20vKiIsCiAgICAiaHR0cHM6Ly9sYWJzLmdvb2dsZS8qIgogIF0sCiAgImNvbnRlbnRfc2NyaXB0cyI6IFsKICAgIHsKICAgICAgIm1hdGNoZXMiOiBbCiAgICAgICAgImh0dHBzOi8vZmxvdy5nb29nbGUuY29tLyoiLAogICAgICAgICJodHRwczovL2xhYnMuZ29vZ2xlLyoiCiAgICAgIF0sCiAgICAgICJqcyI6IFsKICAgICAgICAiZmxvdy5qcyIKICAgICAgXSwKICAgICAgInJ1bl9hdCI6ICJkb2N1bWVudF9zdGFydCIKICAgIH0KICBdCn0K",
    "popup.html": "PCFkb2N0eXBlIGh0bWw+PGh0bWwgbGFuZz0idmkiPjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij48c3R5bGU+Ym9keXtmb250OjE0cHggc3lzdGVtLXVpO3dpZHRoOjI3MHB4O3BhZGRpbmc6MTRweH08L3N0eWxlPjxiPlbhur0gVGF5IMK3IEdvb2dsZSBGbG93PC9iPjxwPlRyb25nIFbhur0gVGF5LCBi4bqlbSBU4buxIHThuqFvIHZpZGVvIMSR4buDIOG7qW5nIGThu6VuZyBt4bufIHRhYiBGbG93IHbDoCB04buxIGvhur90IG7hu5FpLjwvcD48L2h0bWw+Cg=="
}

def sync_chrome_bundle():
    folder = DATA / "chrome-extension"
    try:
        remote = json.loads(patch_read(PATCH_ROOT + "main/companion.json", 16384))
    except (OSError, ValueError):
        remote = BUNDLED_EXTENSION
    ref = remote if version_tuple(remote["version"]) > version_tuple(BUNDLED_EXTENSION["version"]) else BUNDLED_EXTENSION
    revision = ref["commit"]
    names = {"manifest.json", "worker.js", "flow.js", "popup.html"}
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or set(ref["files"]) != names:
        raise ValueError("Danh sách tệp tiện ích không hợp lệ.")
    manifest_path = folder / "manifest.json"
    old = json.loads(manifest_path.read_bytes()) if manifest_path.is_file() else None
    if old and version_tuple(old["version"]) >= version_tuple(ref["version"]):
        return old["version"], False
    content = {}
    for name, expected in ref["files"].items():
        if ref is BUNDLED_EXTENSION:
            data = base64.b64decode(BUNDLED_EXTENSION_FILES[name], validate=True)
        else:
            data = patch_read(PATCH_ROOT + revision + "/chrome-extension/" + name, 1024 * 1024)
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{40}", expected) or blob != expected:
            raise ValueError(f"Tệp tiện ích {name} không khớp mã kiểm tra GitHub.")
        content[name] = data
    manifest = json.loads(content["manifest.json"])
    if manifest["version"] != ref["version"] or manifest["manifest_version"] != 3:
        raise ValueError("Phiên bản tiện ích tải về không khớp.")
    if old:
        permissions = ("permissions", "host_permissions", "optional_permissions", "optional_host_permissions")
        scopes = lambda m: sorted(pattern for script in m.get("content_scripts", []) for pattern in script.get("matches", []))
        if (old["name"] != manifest["name"] or scopes(old) != scopes(manifest)
            or any(sorted(old.get(key, [])) != sorted(manifest.get(key, []))
                   for key in permissions
                   if not (key == "permissions" and ref is BUNDLED_EXTENSION
                           and sorted(old.get(key, [])) == ["alarms", "storage"]
                           and sorted(manifest.get(key, [])) == ["alarms", "debugger", "storage"]))):
            raise ValueError("Bản tiện ích mới thay đổi quyền Chrome và cần xác nhận riêng.")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chrome-patch-", dir=DATA) as temp:
        pending = Path(temp)
        backup = {name: (folder / name).read_bytes() for name in names if (folder / name).is_file()}
        written = []
        try:
            for name in ("worker.js", "flow.js", "popup.html", "manifest.json"):
                path = pending / name
                path.write_bytes(content[name])
                os.replace(path, folder / name)
                written.append(name)
        except OSError:
            for name in reversed(written):
                target = folder / name
                if name in backup:
                    path = pending / name
                    path.write_bytes(backup[name])
                    os.replace(path, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return manifest["version"], True

def prepare_chrome_bundle():
    if not LOCK.acquire(blocking=False):
        return "Đợi tác vụ đang chạy kết thúc trước khi chuẩn bị tiện ích."
    try:
        version, _ = sync_chrome_bundle()
        folder = DATA / "chrome-extension"
        os.startfile(str(folder))
        return (f"Tiện ích {version} tại {folder}. Trong Chrome, mở chrome://extensions và "
                "nạp thư mục này một lần. Giữ nguyên đường dẫn để tự cập nhật sau này.")
    except Exception as error:
        return f"Không chuẩn bị được tiện ích Chrome: {error}"
    finally:
        LOCK.release()

def refresh_existing_extension():
    while True:
        if (DATA / "chrome-extension" / "manifest.json").is_file() and LOCK.acquire(blocking=False):
            try:
                sync_chrome_bundle()
            except (OSError, ValueError, KeyError):
                pass
            finally:
                LOCK.release()
        time.sleep(60)


def update():
    if not LOCK.acquire(blocking=False):
        return "Đợi video xuất xong trước khi cập nhật."
    restart = False
    try:
        manifest = json.loads(patch_read(PATCH_ROOT + "main/update.json", 16384))
        version = manifest["version"]
        companion = ""
        if (DATA / "chrome-extension" / "manifest.json").is_file():
            ext_version, changed = sync_chrome_bundle()
            if changed:
                companion = f" Đã đồng bộ tiện ích Chrome {ext_version}. Chrome sẽ tự nạp lại khi rảnh."
        if version_tuple(version) <= version_tuple(VERSION):
            return f"Vẽ Tay đang ở bản mới nhất ({VERSION})." + companion
        if manifest["abi"] != PATCH_ABI:
            return "Bản này cần nâng cấp thư viện. Tải bộ cài tại trang GitHub Releases."
        revision = manifest["commit"]
        expected = manifest["git_blob"]
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                   for value in (revision, expected)):
            raise ValueError("Mã kiểm tra gói cập nhật không hợp lệ.")
        payload = patch_read(PATCH_ROOT + revision + "/app.py", 1024 * 1024)
        actual = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
        if actual != expected:
            raise ValueError("Gói tải về không khớp mã kiểm tra GitHub.")
        source = payload.decode("utf-8")
        if f'VERSION = "{version}"' not in source.splitlines():
            raise ValueError("Phiên bản trong gói cập nhật không khớp.")
        target = ROOT / "app.py"
        compile(source, str(target), "exec")
        with tempfile.TemporaryDirectory(prefix="vetay-patch-", dir=ROOT) as folder:
            pending = Path(folder) / "app.py"
            pending.write_bytes(payload)
            backup = Path(folder) / "previous.py"
            shutil.copyfile(target, backup)
            os.replace(pending, target)
            try:
                subprocess.Popen([sys.executable, str(target)], cwd=ROOT)
            except OSError:
                os.replace(backup, target)
                raise
        restart = True
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return f"Đã cập nhật nhẹ lên {version}. Đang mở lại Vẽ Tay…"
    except Exception as error:
        return f"Cập nhật thất bại: {error}"
    finally:
        if not restart:
            LOCK.release()


def open_chrome_flow(token):
    candidates = [Path(os.environ[key]) / "Google/Chrome/Application/chrome.exe"
                  for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Không tìm thấy Google Chrome trên máy.")
    project = "https://flow.google.com/#vetay=" + token
    subprocess.Popen([str(chrome), "--new-tab", project])

class ChromeFlowBridge:
    def __init__(self):
        self.token = uuid.uuid4().hex
        self.guard = threading.Lock()
        self.job = None

    def serve(self, port=7877):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def response(self, status, data):
                content = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                import hmac
                if self.path == "/extension-idle":
                    with bridge.guard:
                        idle = bridge.job is None and not LOCK.locked()
                    return self.response(200, {"idle": idle})
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                if self.path not in ("/next", "/batch"):
                    return self.response(404, "Không tìm thấy.")
                with bridge.guard:
                    state = bridge.job
                    index = len(state["paths"]) if state else 0
                    if not state or state["error"] or index >= len(state["prompts"]):
                        return self.response(200, {"done": True})
                    result = {"job": state["id"], "index": index, "prompt": state["prompts"][index]}
                    if self.path == "/batch":
                        result["prompts"] = state["prompts"][index:]
                self.response(200, result)

            def do_POST(self):
                import hmac
                import io
                from PIL import Image, ImageOps
                if not hmac.compare_digest(self.headers.get("X-VeTay-Token", ""), bridge.token):
                    return self.response(403, "Mã kết nối không đúng.")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size <= 0 or size > 24 * 1024 * 1024:
                        raise ValueError("Kích thước dữ liệu không hợp lệ.")
                    self.connection.settimeout(20)
                    with bridge.guard:
                        state = bridge.job
                        if not state or state["error"]:
                            return self.response(409, "Không có tác vụ đang chờ.")
                        index = len(state["paths"])
                        if self.path == "/progress" and size <= 256:
                            progress = json.loads(self.rfile.read(size))
                            count = progress["count"]
                            if type(count) is not int or not 1 <= count <= len(state["prompts"]):
                                return self.response(400, "Số cảnh không hợp lệ.")
                            key = "submitted" if progress.get("stage") == "submitted" else "generated"
                            state[key] = max(state.get(key, 0), count)
                        elif self.path == "/error" and size <= 8192:
                            state["error"] = str(json.loads(self.rfile.read(size))["message"])[:600]
                        elif self.path == f"/image/{state['id']}/{index}" and index < len(state["prompts"]):
                            payload = self.rfile.read(size)
                            if len(payload) != size:
                                raise ValueError("Ảnh chưa tải đủ.")
                            with Image.open(io.BytesIO(payload)) as image:
                                if image.width * image.height > 32_000_000:
                                    raise ValueError("Ảnh quá lớn.")
                                target = state["folder"] / f"chrome-{index:04}.png"
                                ImageOps.exif_transpose(image).convert("RGB").save(target)
                            state["paths"].append(target)
                        else:
                            return self.response(409, "Ảnh không khớp mã hoặc thứ tự cảnh.")
                        state["event"].set()
                    self.response(200, {"received": True})
                except Exception as error:
                    self.response(400, str(error)[:300])

        import socket
        class ExclusiveServer(ThreadingHTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        server = ExclusiveServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def images(self, prompts, folder, report):
        state = {"id": uuid.uuid4().hex, "prompts": prompts, "folder": folder,
                 "paths": [], "event": threading.Event(), "error": None}
        with self.guard:
            self.token = uuid.uuid4().hex
            self.job = state
        try:
            open_chrome_flow(self.token)
            report("Đã mở Google Flow. Đang chờ tiện ích tạo dự án mới…")
            while True:
                if not state["event"].wait(330):
                    raise RuntimeError("Hết thời gian chờ ảnh từ Chrome. Kiểm tra tab Flow do Vẽ Tay mở và phiên bản tiện ích trong chrome://extensions.")
                with self.guard:
                    state["event"].clear()
                    if state["error"]:
                        raise RuntimeError("Google Flow: " + state["error"])
                    count = len(state["paths"])
                    if count == len(prompts):
                        return state["paths"].copy()
                report(f"Đã gửi {state.get('submitted', 0)}/{len(prompts)} prompt. Flow đã tạo {state.get('generated', count)}/{len(prompts)} ảnh. Vẽ Tay đã nhận {count}/{len(prompts)} ảnh.")
        finally:
            with self.guard:
                self.job = None

def ui(check=False):
    prepare_data()
    import gradio as gr
    bridge = ChromeFlowBridge()
    chrome_server = None
    def run(script, voice, pictures, provider, ratio, render, progress=gr.Progress()):
        def report(message):
            progress(None, desc=message)
        if not LOCK.acquire(blocking=False):
            raise gr.Error("Một tác vụ đang chạy.")
        rows = []
        try:
            with tempfile.TemporaryDirectory(prefix="input-", dir=DATA) as folder:
                folder = Path(folder)
                fingerprint = hashlib.sha256()
                if voice:
                    with Path(voice).open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            fingerprint.update(chunk)
                else:
                    fingerprint.update((script or "").encode("utf-8"))
                key = ("voice" if voice else "script", fingerprint.hexdigest())
                cache = DATA / "last-analysis.wav"
                if LAST_ANALYSIS.get("key") == key and cache.is_file():
                    wav, plan = cache, LAST_ANALYSIS["plan"]
                else:
                    yield [], None, "Đang chuẩn bị lời đọc…"
                    wav = prepare_audio(script, voice, folder)
                    yield [], None, "Đang nhận dạng lời đọc và chia cảnh…"
                    plan = scenes(transcribe(wav), duration(wav))
                    os.replace(wav, cache)
                    wav = cache
                    LAST_ANALYSIS.update(key=key, plan=plan)
                rows = scene_rows(plan, ratio)
                yield rows, None, f"Sẵn sàng: {len(rows)} cảnh và prompt."
                if not render:
                    return
                if provider == "chrome":
                    if chrome_server is None:
                        raise RuntimeError("Cổng kết nối Chrome 7877 đang bận. Đóng các bản Vẽ Tay khác rồi mở lại.")
                    yield rows, None, "Đang mở Google Flow và kết nối tự động với tiện ích Chrome…"
                    pictures = bridge.images([row[-1] for row in rows], folder, report)

                if not pictures:
                    raise ValueError("Chọn ảnh theo thứ tự cảnh hoặc chọn Google Flow.")
                dest = DATA / f"VeTay-{uuid.uuid4().hex[:8]}.mp4"
                path = render_folder(wav, pictures, plan, dest, report, ratio)
                yield rows, path, f"Đã xuất MP4 · {len(rows)} cảnh."
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
            yield rows, None, "Lỗi xử lý âm thanh hoặc video: " + detail[-1500:]
        except Exception as error:
            yield rows, None, "Tác vụ dừng: " + str(error)
        finally:
            LOCK.release()

    def analyze(script, voice, ratio):
        yield from run(script, voice, None, None, ratio, False)

    def generate_video(script, voice, pictures, provider, ratio):
        yield from run(script, voice, pictures, provider, ratio, True)

    css = "body, .gradio-container, button, textarea, input { font-family: 'Segoe UI', Arial, sans-serif !important; }"
    with gr.Blocks(title="Vẽ Tay") as app:
        gr.HTML('<div lang="vi"><h1>Vẽ Tay</h1></div>')
        with gr.Row():
            with gr.Column():
                script = gr.Textbox(label="Kịch bản tiếng Việt", lines=5)
                voice = gr.Audio(label="Bản thu lời đọc (MP3 / WAV)", sources=["upload"], type="filepath")

                ratio = gr.Dropdown([(f"{key} · {w} × {h}", key) for key, (w,h) in VIDEO_SIZES.items()],
                                    value="9:16", label="Tỷ lệ video")
            with gr.Column():
                provider = gr.Radio([("Google Flow qua tiện ích Chrome", "chrome"), ("Ảnh có sẵn", "files")],
                                    value="chrome", label="Nguồn ảnh minh họa")

                pictures = gr.File(label="Ảnh có sẵn theo thứ tự cảnh", file_count="multiple",
                                   file_types=["image"], type="filepath")
        with gr.Row():
            analysis = gr.Button("Phân tích và chia cảnh")
            generate = gr.Button("Tự tạo video", variant="primary")
        table = gr.Dataframe(headers=["Cảnh", "Bắt đầu", "Kết thúc", "Thời lượng (giây)",
                                      "Lời đọc", "Prompt Google Flow"],
                             datatype=["number", "str", "str", "number", "str", "str"],
                             label="Bảng phân cảnh theo lời đọc", wrap=True, interactive=False)
        task = gr.Textbox(label="Tiến độ dựng", interactive=False)
        video = gr.Video(label="Video MP4")
        status = gr.Textbox(label="Trạng thái cập nhật", interactive=False)
        gr.Button("Chuẩn bị tiện ích Chrome").click(prepare_chrome_bundle, outputs=status)
        gr.Button("Cập nhật qua GitHub").click(update, outputs=status)
        analysis.click(analyze, [script, voice, ratio], [table, video, task])
        generate.click(generate_video, [script, voice, pictures, provider, ratio], [table, video, task])
    if check:
        engine()
        print(f"VeTay {VERSION}: {len(app.config['components'])} controls.")
        return
    import webview
    try:
        chrome_server = bridge.serve()
        threading.Thread(target=refresh_existing_extension, daemon=True).start()
    except OSError:
        pass
    _, local_url, _ = app.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", prevent_thread_lock=True, inbrowser=False,
        share=False, allowed_paths=[str(DATA)], quiet=True, css=css)
    try:
        webview.create_window("Vẽ Tay", local_url, width=1080, height=900, min_size=(740, 620))
        webview.start(gui="edgechromium")
    finally:
        if chrome_server is not None:
            chrome_server.shutdown()
            chrome_server.server_close()
        app.close()

if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--smoke", nargs=3, metavar=("VOICE", "IMAGE", "OUTPUT"))
    cli.add_argument("--check", action="store_true")
    cli.add_argument("--ratio", choices=VIDEO_SIZES, default="9:16")
    args = cli.parse_args()
    if args.smoke:
        voice, image, out = map(lambda item: Path(item).resolve(), args.smoke)
        render_folder(voice, [image], [(0, 48, "Smoke test")], out, print, args.ratio)
    else:
        ui(check=args.check)

