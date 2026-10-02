"""Micophone receiver engine: plays an Android phone's microphone on this PC.

Run headless:  python receiver.py [--device NAME|INDEX] [--latency-ms 40]
GUI:           python micophone_gui.py

Protocol v2 (must match android/app/src/main/java/com/farzonline/micophone/Protocol.kt):
  Port 50005: UDP on all interfaces (Wi-Fi), TCP on 127.0.0.1 (USB via `adb reverse`).
  Audio: 16-bit signed little-endian mono PCM; rate (48000, or 44100 fallback) sent in HELLO.
  UDP datagrams are b"PM" + type byte + body:
    AUDIO    1  seq:u16le + 10 ms of PCM               phone -> PC
    DISCOVER 2  (empty)                                phone -> broadcast
    ANNOUNCE 3  utf8 PC name                           PC -> phone
    HELLO    4  rate:u32le + utf8 phone name, 1/s      phone -> PC
    ACK      5  utf8 PC name   (you are the live mic)  PC -> phone
    BUSY     6  utf8 PC name   (another phone is live) PC -> phone
  TCP: phone sends b"PM" + HELLO + rate:u32le + len:u8 + name, then raw PCM.
       PC sends type:u8 (ACK/BUSY) + len:u8 + utf8 PC name, once a second.
"""
import argparse
import io
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import sounddevice as sd

PORT = 50005
MAGIC = b"PM"
AUDIO, DISCOVER, ANNOUNCE, HELLO, ACK, BUSY = range(1, 7)
SOURCE_TIMEOUT = 2.5  # seconds of silence before a Wi-Fi phone counts as gone
LATENCY_PRESETS = {"Low (20 ms)": 20, "Normal (40 ms)": 40, "Stable (80 ms)": 80}
PC_NAME = socket.gethostname()
PC_NAME_B = PC_NAME.encode()[:64]
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Micophone"
ADB_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flashes from the GUI exe


def msg(kind, body=b""):
    return MAGIC + bytes([kind]) + body


class JitterBuffer:
    """Absorbs Wi-Fi jitter and phone/PC clock drift without audible glitches.

    - Starts, and restarts after an underrun, only once `target` audio is queued.
    - Drift: when the smoothed fill level leaves [target/2, 1.5*target], drop or repeat
      one sample at most every 50 ms. Inaudible; corrects up to ~400 ppm.
    - Bursts past 3x target (Wi-Fi stall, then flush) are cut back to target, so
      latency never creeps up.
    """

    def __init__(self, rate, target_ms):
        self.target = int(rate * target_ms / 1000) * 2
        self.limit = self.target * 3
        self.slip_every = int(rate * 0.05) * 2
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.priming = True
        self.avg = float(self.target)
        self.since_slip = 0
        self.underruns = self.overruns = self.slips = 0

    def push(self, data):
        with self.lock:
            self.buf += data
            if len(self.buf) > self.limit:
                del self.buf[: len(self.buf) - self.target]
                self.overruns += 1

    def pull(self, n):
        with self.lock:
            if self.priming:
                if len(self.buf) < self.target:
                    return bytes(n)
                self.priming = False
                self.avg = float(len(self.buf))
            self.avg += (len(self.buf) - self.avg) * 0.05
            take = n
            self.since_slip += n
            if self.since_slip >= self.slip_every and n >= 4:
                if self.avg > self.target * 1.5:
                    take = n + 2
                elif self.avg < self.target * 0.5:
                    take = n - 2
                if take != n:
                    self.since_slip = 0
                    self.slips += 1
            out = bytes(self.buf[:take])
            del self.buf[:take]
            if len(out) < take:
                self.priming = True
                self.underruns += 1
                out = out[:n]
                return out + bytes(n - len(out))
        mid = n // 4 * 2  # even byte offset near the middle
        if take > n:
            return out[:mid] + out[mid + 2:]  # drop one sample
        if take < n:
            return out[:mid] + out[mid - 2:mid] + out[mid:]  # repeat one sample
        return out

    def level_ms(self, rate):
        return len(self.buf) / 2 / rate * 1000


class Source:
    """The one phone currently playing. `addr` is a UDP (ip, port) or a TCP socket."""

    def __init__(self, kind, addr, name, rate):
        self.kind, self.addr, self.name, self.rate = kind, addr, name, rate
        self.last_seen = time.monotonic()
        self.seq = None
        self.received = self.lost = 0
        self.last_pcm = b""

    def label(self):
        return f"{self.name} via USB" if self.kind == "usb" else f"{self.name} via Wi-Fi ({self.addr[0]})"


def output_devices():
    """WASAPI outputs: full names and ~10x lower latency than MME. Others only if WASAPI is missing."""
    apis = sd.query_hostapis()
    devs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
    wasapi = [d for d in devs if "WASAPI" in apis[d["hostapi"]]["name"]]
    return wasapi or devs


def resolve_output(name):
    """Device index for `name`; None (auto) prefers VB-CABLE, then the default output.

    A named device that's missing raises instead of falling back to speakers:
    speakers + phone mic in the same room = feedback.
    """
    devs = output_devices()
    if not devs:
        raise ValueError("no audio output devices found")
    if name:
        for d in devs:
            if d["name"] == name:
                return d["index"]
        for d in devs:
            if name.lower() in d["name"].lower():
                return d["index"]
        raise ValueError(f"output '{name}' not found")
    for d in devs:
        if d["name"].startswith("CABLE Input"):
            return d["index"]
    default = sd.query_hostapis(devs[0]["hostapi"])["default_output_device"]
    return default if default >= 0 else devs[0]["index"]


def local_ips():
    """This PC's LAN addresses, the one with the default route first."""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; just picks the route
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips += [ai[4][0] for ai in socket.getaddrinfo(PC_NAME, None, socket.AF_INET)]
    except OSError:
        pass
    return [ip for i, ip in enumerate(ips) if ip not in ips[:i] and not ip.startswith(("127.", "169.254."))]


def find_adb():
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for p in (shutil.which("adb"), APP_DIR / "platform-tools" / "adb.exe",
              local / "Android" / "Sdk" / "platform-tools" / "adb.exe",
              local / "Microsoft" / "WinGet" / "Links" / "adb.exe"):
        if p and Path(p).is_file():
            return str(p)
    return None


def install_adb():
    """Download Google's official Android platform-tools into %LOCALAPPDATA%\\Micophone."""
    with urllib.request.urlopen(ADB_URL, timeout=120) as r:
        data = r.read()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(APP_DIR)


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


class Engine:
    def __init__(self, port=PORT, device=None, latency_ms=40, usb_tunnel=True, log=print):
        self.port = port
        self.device = device  # output device name; None = auto
        self.latency_ms = latency_ms
        self.usb_tunnel = usb_tunnel
        self.log = log
        self.lock = threading.Lock()  # source / jb
        self.pa_lock = threading.Lock()  # PortAudio calls
        self.stopped = threading.Event()
        self.source = None
        self.jb = None
        self.stream = None
        self.stream_rate = None
        self.output_name = ""
        self.last_callback = 0.0
        self.next_open = 0.0
        self.reopen = False
        self.refresh = False
        self.peak = 0
        self.output_glitches = 0
        self.usb_status = "USB: starting..."
        self.threads = []

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        """Raises OSError if the port is taken (another Micophone is running)."""
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            self.udp.ioctl(socket.SIO_UDP_CONNRESET, False)  # ignore ICMP "port unreachable"
        self.udp.bind(("0.0.0.0", self.port))
        self.udp.settimeout(0.5)
        try:
            self.tcp = socket.create_server(("127.0.0.1", self.port))
        except OSError:
            self.udp.close()
            raise
        self.tcp.settimeout(0.5)
        loops = [self._udp_loop, self._tcp_loop, self._supervise]
        if self.usb_tunnel:
            loops.append(self._adb_loop)
        self.threads = [threading.Thread(target=f, daemon=True) for f in loops]
        for t in self.threads:
            t.start()

    def stop(self):
        self.stopped.set()
        for t in self.threads:
            t.join(2)
        self.udp.close()
        self.tcp.close()

    # ---- controls (any thread) ------------------------------------------

    def set_device(self, name):
        self.device = name
        self.reopen = True

    def set_latency(self, ms):
        self.latency_ms = ms
        with self.lock:
            if self.source:
                self.jb = JitterBuffer(self.source.rate, ms)

    def refresh_devices(self):
        """Re-scan audio devices (PortAudio only sees devices present at startup)."""
        self.refresh = True

    def output_names(self):
        with self.pa_lock:
            return [d["name"] for d in output_devices()]

    def resolved_output(self):
        with self.pa_lock:
            try:
                return sd.query_devices(resolve_output(self.device))["name"]
            except (ValueError, sd.PortAudioError):
                return None

    def take_peak(self):
        p, self.peak = self.peak, 0
        return p / 32768

    def snapshot(self):
        with self.lock:
            src, jb = self.source, self.jb
            if not src:
                return {"source": None, "usb": self.usb_status}
            total = src.received + src.lost
            return {
                "source": src.label(),
                "rate": src.rate,
                "buffer_ms": jb.level_ms(src.rate) if jb else 0,
                "loss": src.lost / total if total else 0.0,
                "glitches": (jb.underruns + jb.overruns if jb else 0) + self.output_glitches,
                "output": self.output_name,
                "usb": self.usb_status,
            }

    # ---- network ---------------------------------------------------------

    def _claim(self, kind, addr, name, rate):
        """Make this phone the live source if free. True = it's live."""
        now = time.monotonic()
        with self.lock:
            src = self.source
            same = src is not None and src.kind == kind and src.addr == addr
            if src and not same:
                stale = now - src.last_seen > SOURCE_TIMEOUT
                restarted = kind == src.kind == "wifi" and src.addr[0] == addr[0]  # same phone, new socket
                if not (stale or restarted):
                    return False
            if not same or src.rate != rate:
                self.source = Source(kind, addr, name, rate)
                self.jb = JitterBuffer(rate, self.latency_ms)
                self.log(f"Connected: {self.source.label()}, {rate} Hz")
            self.source.last_seen = now
            return True

    def _release(self, addr, why):
        with self.lock:
            if self.source and self.source.addr is addr:
                self.log(f"Disconnected: {self.source.name} ({why})")
                self.source = self.jb = None

    def _meter(self, pcm):
        if len(pcm) >= 2:
            s = memoryview(pcm[: len(pcm) // 2 * 2]).cast("h")
            self.peak = max(self.peak, max(s), -min(s))

    def _udp_loop(self):
        while not self.stopped.is_set():
            try:
                data, addr = self.udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self.stopped.is_set():
                    return
                continue
            if len(data) < 3 or data[:2] != MAGIC:
                continue
            kind, body = data[2], data[3:]
            if kind == AUDIO:
                self._on_audio(addr, body)
            elif kind == HELLO and len(body) >= 4:
                rate = struct.unpack_from("<I", body)[0]
                if not 8000 <= rate <= 96000:
                    continue
                live = self._claim("wifi", addr, body[4:68].decode("utf-8", "replace"), rate)
                self._send(msg(ACK if live else BUSY, PC_NAME_B), addr)
            elif kind == DISCOVER:
                self._send(msg(ANNOUNCE, PC_NAME_B), addr)

    def _send(self, data, addr):
        try:
            self.udp.sendto(data, addr)
        except OSError:
            pass  # phone's Wi-Fi blipped; it retries every second

    def _on_audio(self, addr, body):
        if len(body) <= 2 or len(body) % 2:
            return
        seq = int.from_bytes(body[:2], "little")
        pcm = body[2:]
        with self.lock:
            src, jb = self.source, self.jb
            if not src or src.kind != "wifi" or src.addr != addr:
                return
            src.last_seen = time.monotonic()
            if src.seq is not None:
                gap = (seq - src.seq) & 0xFFFF
                if gap == 0 or gap > 0x8000:
                    return  # duplicate, or late and already concealed
                if gap > 1:
                    src.lost += gap - 1
                    if gap <= 3:  # short loss: repeating 10-20 ms of voice beats a click
                        for _ in range(gap - 1):
                            jb.push(src.last_pcm)
            src.seq = seq
            src.received += 1
            src.last_pcm = pcm
        jb.push(pcm)
        self._meter(pcm)

    def _tcp_loop(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.tcp.accept()
            except OSError:  # includes timeout
                continue
            threading.Thread(target=self._usb_client, args=(conn,), daemon=True).start()

    def _usb_client(self, conn):
        with conn:
            try:
                conn.settimeout(3)
                head = recv_exact(conn, 8)
                if head[:3] != msg(HELLO):
                    return
                rate = struct.unpack_from("<I", head, 3)[0]
                name = recv_exact(conn, head[7]).decode("utf-8", "replace")
                if not 8000 <= rate <= 96000:
                    return
                conn.settimeout(1)
                odd, next_ack = b"", 0.0
                while not self.stopped.is_set():
                    live = self._claim("usb", conn, name, rate)
                    if time.monotonic() >= next_ack:
                        conn.sendall(bytes([ACK if live else BUSY, len(PC_NAME_B)]) + PC_NAME_B)
                        next_ack = time.monotonic() + 1
                    try:
                        data = conn.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    if not live:
                        continue
                    data = odd + data  # TCP splits anywhere; carry half a sample over
                    cut = len(data) - len(data) % 2
                    jb = self.jb
                    if jb:
                        jb.push(data[:cut])
                    odd = data[cut:]
                    self._meter(data[:cut])
            except (OSError, ConnectionError):
                pass
            finally:
                self._release(conn, "USB link closed")

    def _adb_loop(self):
        """Keeps `adb reverse` set up for every authorised phone: plug in and it works."""
        warned = None
        while not self.stopped.wait(0 if warned is None else 2):
            adb = find_adb()
            if not adb:
                self.usb_status = "USB: adb not installed"
                warned = True
                continue
            try:
                run = lambda *a: subprocess.run([adb, *a], capture_output=True, text=True,
                                                timeout=10, creationflags=NO_WINDOW).stdout
                lines = run("devices").splitlines()[1:]
                phones = [l.split("\t")[0] for l in lines if l.endswith("\tdevice")]
                for serial in phones:
                    if f"tcp:{self.port} tcp:{self.port}" not in run("-s", serial, "reverse", "--list"):
                        run("-s", serial, "reverse", f"tcp:{self.port}", f"tcp:{self.port}")
                        self.log(f"USB tunnel ready for phone {serial}")
                if phones:
                    self.usb_status = f"USB: ready ({len(phones)} phone{'s' * (len(phones) > 1)} connected)"
                elif any(l.endswith("\tunauthorized") for l in lines):
                    self.usb_status = "USB: tap 'Allow' on the phone's USB debugging prompt"
                else:
                    self.usb_status = "USB: no phone (connect a cable, USB debugging on)"
            except (OSError, subprocess.SubprocessError) as e:
                self.usb_status = f"USB: adb error ({e})"
            warned = False

    # ---- audio output ----------------------------------------------------

    def _callback(self, outdata, frames, time_info, status):
        self.last_callback = time.monotonic()
        if status.output_underflow:
            self.output_glitches += 1
        jb = self.jb
        outdata[:] = jb.pull(frames * 2) if jb else bytes(frames * 2)

    def _supervise(self):
        """Owns the output stream: opens it while a phone is live, reopens on device
        change / rate change / stall (device unplugged), retries every 2 s on failure."""
        # WASAPI needs COM on the thread that opens streams, and PortAudio only sets it up
        # in the thread that initialises it. So initialise here, where all streams live.
        self._reinit_portaudio()
        while not self.stopped.wait(0.2):
            now = time.monotonic()
            with self.lock:
                src = self.source
                if src and src.kind == "wifi" and now - src.last_seen > SOURCE_TIMEOUT:
                    self.log(f"Lost {src.name}: no audio for {SOURCE_TIMEOUT:.0f} s, waiting for it to return")
                    self.source = self.jb = src = None
            want = src.rate if src else None
            stalled = self.stream is not None and now - self.last_callback > 1.5
            if stalled:
                self.log("Audio output stopped responding (device unplugged?), reopening")
            if self.stream and (want != self.stream_rate or self.reopen or stalled or self.refresh):
                self._close_stream()
            if self.refresh or stalled:
                self._reinit_portaudio()
            if want and not self.stream and now >= self.next_open:
                self._open_stream(want)
        self._close_stream()

    def _reinit_portaudio(self):
        with self.pa_lock:
            sd._terminate()
            sd._initialize()
        self.refresh = False

    def _open_stream(self, rate):
        self.reopen = False
        with self.pa_lock:
            try:
                idx = resolve_output(self.device)
                info = sd.query_devices(idx)
                api = sd.query_hostapis(info["hostapi"])["name"]
                extra = sd.WasapiSettings(auto_convert=True) if "WASAPI" in api else None
                stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", device=idx,
                                            latency="low", callback=self._callback, extra_settings=extra)
                stream.start()
            except (ValueError, sd.PortAudioError) as e:
                self.log(f"Can't open audio output: {e}. Retrying...")
                self.next_open = time.monotonic() + 2
                self.refresh = True  # maybe it was just plugged in
                return
        self.last_callback = time.monotonic()
        self.stream, self.stream_rate, self.output_name = stream, rate, info["name"]
        self.log(f"Playing to '{info['name']}'")

    def _close_stream(self):
        if self.stream:
            with self.pa_lock:
                try:
                    self.stream.abort()
                    self.stream.close()
                except sd.PortAudioError:
                    pass
            self.stream = self.stream_rate = None
            self.output_name = ""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--device", help="output device name (or index from --list-devices); default: VB-CABLE if installed")
    p.add_argument("--latency-ms", type=float, default=40,
                   help="jitter buffer; lower = less delay, higher = fewer dropouts on busy Wi-Fi")
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()

    if args.list_devices:
        for d in output_devices():
            print(f"{d['index']:3}  {d['name']}")
        return
    device = args.device
    if device and device.isdigit():
        device = sd.query_devices(int(device))["name"]

    eng = Engine(args.port, device, args.latency_ms)
    try:
        eng.start()
    except OSError:
        sys.exit(f"Port {args.port} is in use. Is Micophone already running?")
    print(f"Micophone on port {args.port}. This PC: {PC_NAME} {', '.join(local_ips())}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()


if __name__ == "__main__":
    main()
