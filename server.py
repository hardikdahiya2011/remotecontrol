"""
Remote PC Control Server
========================
Real-time WebSocket server for remote PC control from Android.

Features:
  - Screen streaming (20 FPS, high quality)
  - Mouse & keyboard control
  - Text-to-Speech (Windows SAPI, no install needed)
  - YouTube audio — ad-free, no VLC, no ffplay needed
  - Three independent volume controls: TTS / YouTube / System
  - No console window
  - Auto-starts on every Windows boot
"""

import asyncio
import websockets
import websockets.exceptions
import json
import threading
import sys
import os
import io
import time
import logging
import winreg
import ctypes
import numpy as np
import subprocess

# ── Suppress ALL subprocess console windows (fixes blue flash from pyttsx3 etc.)
if sys.platform == "win32":
    _orig_popen = subprocess.Popen.__init__
    def _silent_popen(self, args, **kwargs):
        kwargs.setdefault("creationflags", 0)
        kwargs["creationflags"] |= 0x08000000  # CREATE_NO_WINDOW
        _orig_popen(self, args, **kwargs)
    subprocess.Popen.__init__ = _silent_popen

import mss
from PIL import Image
import pynput.mouse
import pynput.keyboard
import pyttsx3
import pystray
import av
import sounddevice as sd
import yt_dlp
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from comtypes import CLSCTX_ALL

# ── Config ─────────────────────────────────────────────────────────────────────
PORT         = 8765
PASSWORD     = ""
STREAM_FPS   = 20
JPEG_QUALITY = 85
APP_NAME     = "RemoteControl"
INSTALL_DIR  = os.path.join(os.environ.get("APPDATA", "."), APP_NAME)
LOG_FILE     = os.path.join(INSTALL_DIR, "server.log")

# Firebase Realtime Database URL (used for auto-discovery in Android app)
FIREBASE_URL = "https://remotecontrol-6935b-default-rtdb.firebaseio.com"


def setup_logging():
    os.makedirs(INSTALL_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def _report_to_firebase():
    """Post this PC's IP + hostname to Firebase so the Android app can find it."""
    import socket
    import urllib.request
    import json as _json

    try:
        hostname = socket.gethostname()
        # Get the local IP that can reach the internet (not 127.0.0.1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()

        # Use hostname as the unique key (safe chars only)
        key = "".join(c if c.isalnum() or c in "-_" else "_" for c in hostname)

        payload = _json.dumps({
            "name": hostname,
            "ip":   local_ip,
            "port": PORT,
            "ts":   int(time.time() * 1000),   # milliseconds timestamp
        }).encode("utf-8")

        url = f"{FIREBASE_URL}/pcs/{key}.json"
        req = urllib.request.Request(url, data=payload, method="PUT",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        logging.info(f"Reported to Firebase: {hostname} @ {local_ip}")
    except Exception as e:
        logging.warning(f"Firebase report failed (offline?): {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  Screen Streamer
# ══════════════════════════════════════════════════════════════════════════════

class ScreenStreamer:
    def __init__(self):
        self._sct     = None
        self._monitor = None

    def init(self):
        self._sct     = mss.mss()
        self._monitor = self._sct.monitors[1]

    @property
    def width(self)  -> int: return self._monitor["width"]  if self._monitor else 1920
    @property
    def height(self) -> int: return self._monitor["height"] if self._monitor else 1080

    def capture(self) -> bytes:
        shot = self._sct.grab(self._monitor)
        img  = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf  = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  Input Controller
# ══════════════════════════════════════════════════════════════════════════════

class InputController:
    def __init__(self):
        self._mouse    = pynput.mouse.Controller()
        self._keyboard = pynput.keyboard.Controller()
        self._sw = ctypes.windll.user32.GetSystemMetrics(0)
        self._sh = ctypes.windll.user32.GetSystemMetrics(1)

    def _px(self, nx, ny):
        return int(nx * self._sw), int(ny * self._sh)

    def _btn(self, name):
        return pynput.mouse.Button.right if name == "right" else pynput.mouse.Button.left

    def mouse_move(self, nx, ny):
        self._mouse.position = self._px(nx, ny)

    def mouse_down(self, nx, ny, button="left"):
        self.mouse_move(nx, ny)
        self._mouse.press(self._btn(button))

    def mouse_up(self, nx, ny, button="left"):
        self.mouse_move(nx, ny)
        self._mouse.release(self._btn(button))

    def mouse_click(self, nx, ny, button="left", double=False):
        self.mouse_move(nx, ny)
        self._mouse.click(self._btn(button), 2 if double else 1)

    def mouse_scroll(self, nx, ny, dx=0, dy=-3):
        self.mouse_move(nx, ny)
        self._mouse.scroll(dx, dy)

    def key_press(self, key):
        try:
            if len(key) == 1:
                self._keyboard.press(key)
                self._keyboard.release(key)
            else:
                k = getattr(pynput.keyboard.Key, key.lower(), None)
                if k:
                    self._keyboard.press(k)
                    self._keyboard.release(k)
        except Exception as e:
            logging.warning(f"key_press: {e}")

    def key_combo(self, modifier, key):
        try:
            mod = getattr(pynput.keyboard.Key, modifier.lower(), pynput.keyboard.Key.ctrl)
            with self._keyboard.pressed(mod):
                self._keyboard.press(key)
                self._keyboard.release(key)
        except Exception as e:
            logging.warning(f"key_combo: {e}")

    def type_text(self, text):
        try:
            self._keyboard.type(text)
        except Exception as e:
            logging.warning(f"type_text: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  TTS Engine
# ══════════════════════════════════════════════════════════════════════════════

class TTSEngine:
    def __init__(self):
        self._lock   = threading.Lock()
        self._volume = 1.0
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", 175)
        self._engine.setProperty("volume", self._volume)

    def speak(self, text: str):
        with self._lock:
            self._engine.setProperty("volume", self._volume)
            self._engine.say(text)
            self._engine.runAndWait()

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))

    @property
    def volume(self) -> float:
        return self._volume


# ══════════════════════════════════════════════════════════════════════════════
#  YouTube Audio Player  (PyAV + sounddevice — no VLC, no ffplay)
# ══════════════════════════════════════════════════════════════════════════════

class YTPlayer:
    def __init__(self):
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._thread      = None
        self._volume      = 0.8
        self._current_url = ""

    def play(self, url: str):
        with self._lock:
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3)
            self._stop_event.clear()
            self._current_url = url
            self._thread = threading.Thread(
                target=self._play_thread, args=(url,), daemon=True
            )
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))

    @property
    def volume(self) -> float:
        return self._volume

    def _get_audio_url(self, url: str):
        opts = {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Try to get best audio-only stream
                formats = info.get("formats", [])
                audio_only = [
                    f for f in formats
                    if f.get("vcodec") in ("none", None, "")
                    and f.get("acodec") not in ("none", None, "")
                    and f.get("url")
                ]
                if audio_only:
                    # prefer highest quality audio
                    best = max(audio_only, key=lambda f: f.get("abr") or f.get("tbr") or 0)
                    return best["url"]
                # Fallback: use any stream url
                return info.get("url")
        except Exception as e:
            logging.error(f"yt-dlp error: {e}")
            return None

    def _play_thread(self, url: str):
        logging.info(f"YT: fetching audio URL for {url}")
        audio_url = self._get_audio_url(url)
        if not audio_url:
            logging.error("YT: could not get audio URL")
            return
        logging.info("YT: starting playback")
        try:
            container = av.open(
                audio_url,
                options={"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5"}
            )
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
            if not audio_stream:
                logging.error("YT: no audio stream found")
                return
            rate      = audio_stream.codec_context.sample_rate or 44100
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)
            with sd.OutputStream(samplerate=rate, channels=2, dtype="float32") as out:
                for frame in container.decode(audio=0):
                    if self._stop_event.is_set():
                        break
                    for resampled in resampler.resample(frame):
                        data = resampled.to_ndarray() * self._volume
                        out.write(np.ascontiguousarray(data.T, dtype=np.float32))
            logging.info("YT: playback finished")
        except Exception as e:
            if not self._stop_event.is_set():
                logging.error(f"YT playback error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  Master Volume
# ══════════════════════════════════════════════════════════════════════════════

class MasterVolume:
    def __init__(self):
        self._iface = None
        try:
            devices = AudioUtilities.GetSpeakers()
            iface   = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._iface = iface.QueryInterface(IAudioEndpointVolume)
        except Exception as e:
            logging.warning(f"MasterVolume init: {e}")

    def set(self, vol: float):
        if self._iface:
            self._iface.SetMasterVolumeLevelScalar(max(0.0, min(1.0, vol)), None)


# ══════════════════════════════════════════════════════════════════════════════
#  WebSocket Server
# ══════════════════════════════════════════════════════════════════════════════

class RemoteServer:
    def __init__(self):
        self._streamer = ScreenStreamer()
        self._input    = InputController()
        self._tts      = TTSEngine()
        self._yt       = YTPlayer()
        self._master   = MasterVolume()

    async def _handler(self, websocket):
        addr = websocket.remote_address
        logging.info(f"Client connected: {addr}")
        if PASSWORD:
            try:
                raw  = await asyncio.wait_for(websocket.recv(), timeout=8.0)
                data = json.loads(raw)
                if data.get("password") != PASSWORD:
                    await websocket.send(json.dumps({"type": "auth", "ok": False}))
                    return
            except Exception:
                return

        self._streamer.init()
        await websocket.send(json.dumps({
            "type": "init", "ok": True,
            "width": self._streamer.width,
            "height": self._streamer.height,
        }))

        stream_task = asyncio.create_task(self._stream_screen(websocket))
        try:
            async for msg in websocket:
                if isinstance(msg, str):
                    await self._dispatch(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            stream_task.cancel()
            logging.info(f"Client disconnected: {addr}")

    async def _stream_screen(self, websocket):
        interval = 1.0 / STREAM_FPS
        loop     = asyncio.get_event_loop()
        try:
            while True:
                t0    = loop.time()
                frame = await loop.run_in_executor(None, self._streamer.capture)
                try:
                    await websocket.send(frame)
                except websockets.exceptions.ConnectionClosed:
                    break
                wait = interval - (loop.time() - t0)
                if wait > 0:
                    await asyncio.sleep(wait)
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, raw: str):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = d.get("type", "")
        if   t == "mouse_move"   : self._input.mouse_move(d["x"], d["y"])
        elif t == "mouse_down"   : self._input.mouse_down(d["x"], d["y"], d.get("button","left"))
        elif t == "mouse_up"     : self._input.mouse_up(d["x"], d["y"], d.get("button","left"))
        elif t == "mouse_click"  : self._input.mouse_click(d["x"], d["y"], d.get("button","left"), d.get("double",False))
        elif t == "mouse_scroll" : self._input.mouse_scroll(d["x"], d["y"], d.get("dx",0), d.get("dy",-3))
        elif t == "key_press"    : self._input.key_press(d["key"])
        elif t == "key_combo"    : self._input.key_combo(d.get("modifier","ctrl"), d["key"])
        elif t == "type_text"    : threading.Thread(target=self._input.type_text, args=(d["text"],), daemon=True).start()
        elif t == "tts_speak"    :
            self._tts.set_volume(d.get("volume", self._tts.volume))
            threading.Thread(target=self._tts.speak, args=(d["text"],), daemon=True).start()
        elif t == "tts_volume"   : self._tts.set_volume(d["value"])
        elif t == "yt_play"      :
            self._yt.set_volume(d.get("volume", self._yt.volume))
            threading.Thread(target=self._yt.play, args=(d["url"],), daemon=True).start()
        elif t == "yt_stop"      : self._yt.stop()
        elif t == "yt_volume"    : self._yt.set_volume(d["value"])
        elif t == "master_volume": self._master.set(d["value"])

    def run(self):
        _register_startup()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _main():
            async with websockets.serve(
                self._handler, "0.0.0.0", PORT,
                max_size=None, ping_interval=20, ping_timeout=10
            ):
                logging.info(f"Server listening on port {PORT}")
                await asyncio.Future()
        loop.run_until_complete(_main())


# ══════════════════════════════════════════════════════════════════════════════
#  Startup Registration
# ══════════════════════════════════════════════════════════════════════════════

def _register_startup():
    try:
        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}"'
        else:
            python_dir = os.path.dirname(sys.executable)
            pythonw    = os.path.join(python_dir, "pythonw.exe")
            script     = os.path.abspath(__file__)
            cmd = f'"{pythonw}" "{script}"' if os.path.isfile(pythonw) else f'"{sys.executable}" "{script}"'
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
    except Exception as e:
        logging.warning(f"Startup registration failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  System Tray
# ══════════════════════════════════════════════════════════════════════════════

def _make_icon():
    from PIL import ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(33, 150, 243, 255))
    draw.text((20, 14), "R", fill=(255, 255, 255, 255))
    return img


def run_tray(server: RemoteServer):
    icon_img = _make_icon()
    def on_quit(icon, _):
        server._yt.stop()
        icon.stop()
        os._exit(0)
    menu = pystray.Menu(
        pystray.MenuItem(f"{APP_NAME}  —  port {PORT}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, icon_img, f"{APP_NAME} (port {PORT})", menu)
    threading.Thread(target=server.run, daemon=True).start()
    icon.run()


def main():
    setup_logging()
    logging.info("=== RemoteControl starting ===")
    # Report IP to Firebase in background (won't block startup if offline)
    threading.Thread(target=_report_to_firebase, daemon=True).start()
    run_tray(RemoteServer())


if __name__ == "__main__":
    main()
