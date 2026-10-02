"""Self-check for the receiver. Run: python test_receiver.py"""
import socket
import struct
import time

import receiver as rx
from receiver import JitterBuffer

RATE = 48000
PKT = 960  # 10 ms at 48 kHz


def test_jitter_buffer():
    jb = JitterBuffer(RATE, target_ms=20)  # target 1920 bytes, limit 5760
    pkt = b"\x01\x00" * (PKT // 2)

    # Priming: silence until the target is queued.
    jb.push(pkt)
    assert jb.pull(100) == bytes(100)
    jb.push(pkt)
    assert jb.pull(100) == pkt[:100]

    # Underrun: partial audio padded with silence, then re-primes.
    jb.buf.clear()
    jb.buf += b"\x01" * 50
    assert jb.pull(100) == b"\x01" * 50 + bytes(50) and jb.underruns == 1 and jb.priming

    # Burst past the limit is cut back to target, keeping whole samples.
    for _ in range(7):
        jb.push(pkt)
    assert len(jb.buf) == jb.target and jb.overruns == 1

    # Drift: a phone running fast keeps the buffer high -> samples get dropped,
    # but never more than one per 50 ms, and output length is always exact.
    jb = JitterBuffer(RATE, target_ms=20)
    for _ in range(4):
        jb.push(pkt)
    for _ in range(200):  # 2 s of 10 ms callbacks, 10% extra audio arriving
        jb.push(pkt + pkt[:96])
        assert len(jb.pull(PKT)) == PKT
    assert 30 <= jb.slips <= 40, jb.slips  # 2 s / 50 ms = 40 max


def test_protocol():
    port = 50555
    logs = []
    eng = rx.Engine(port=port, usb_tunnel=False, log=logs.append)
    eng.start()
    try:
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b.bind(("127.0.0.2", 0))  # a different "phone" needs a different IP
        for s in (a, b):
            s.settimeout(1)
        pc = ("127.0.0.1", port)

        a.sendto(rx.msg(rx.DISCOVER), pc)
        assert a.recv(100) == rx.msg(rx.ANNOUNCE, rx.PC_NAME_B)

        hello = lambda name: rx.msg(rx.HELLO, struct.pack("<I", RATE) + name)
        a.sendto(hello(b"phone A"), pc)
        assert a.recv(100)[2] == rx.ACK
        b.sendto(hello(b"phone B"), pc)
        assert b.recv(100)[2] == rx.BUSY  # one live phone at a time

        # Audio with one lost packet (seq 2): concealed and counted.
        for seq in (0, 1, 3, 4):
            a.sendto(rx.msg(rx.AUDIO, struct.pack("<H", seq) + b"\x10\x00" * (PKT // 2)), pc)
        time.sleep(0.2)
        snap = eng.snapshot()
        assert snap["source"].startswith("phone A") and abs(snap["loss"] - 0.2) < 1e-9, snap

        # The output opens on the first try (WASAPI needs COM on the opening thread).
        # Skipped on machines without a sound card (CI runners).
        time.sleep(0.6)
        if rx.output_devices():
            assert any(m.startswith("Playing to") for m in logs), logs
            assert not any("Can't open" in m for m in logs), logs
        else:
            assert any("no audio output" in m for m in logs), logs

        # Phone A restarts its app (same IP, new port): takes over at once, no 2.5 s wait.
        a2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a2.settimeout(1)
        a2.sendto(hello(b"phone A"), pc)
        assert a2.recv(100)[2] == rx.ACK

        # USB: framed hello over TCP gets BUSY while phone A is live.
        with socket.create_connection(pc, timeout=2) as t:
            t.sendall(rx.msg(rx.HELLO, struct.pack("<I", RATE) + bytes([5]) + b"phone"))
            assert t.recv(2)[0] == rx.BUSY
    finally:
        eng.stop()


test_jitter_buffer()
test_protocol()
print("ok")
