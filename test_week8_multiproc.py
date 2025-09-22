#!/usr/bin/env python3
import json
import os
import sys
import time
import multiprocessing as mp
from types import SimpleNamespace

# Force fork so children inherit our monkeypatches (macOS defaults to spawn)
try:
    mp.set_start_method('fork', force=True)
except RuntimeError:
    pass

# Stub bluetooth before importing Week_8
class _StubBluetoothSocket:
    def __init__(self, *args, **kwargs):
        pass
    def bind(self, *args, **kwargs):
        pass
    def listen(self, *args, **kwargs):
        pass
    def getsockname(self):
        return (None, 1)
    def accept(self):
        raise RuntimeError("Bluetooth not available in offline test")
    def close(self):
        pass
    def shutdown(self, *args, **kwargs):
        pass

_stub_bluetooth = SimpleNamespace(
    BluetoothSocket=_StubBluetoothSocket,
    RFCOMM=1,
    PORT_ANY=0,
    SERIAL_PORT_CLASS=1,
    SERIAL_PORT_PROFILE=1,
    advertise_service=lambda *args, **kwargs: None,
)
sys.modules.setdefault('bluetooth', _stub_bluetooth)

from Week_8 import RaspberryPi, AndroidMessage
import Week_8 as wk8

# Fake requests for all processes (since we fork, assign directly on module)
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode('utf-8')

def _fake_get(url, timeout=None):
    if url.endswith('/status'):
        return _FakeResponse(200, {"ok": True})
    if url.endswith('/stitch'):
        return _FakeResponse(200, {"ok": True})
    return _FakeResponse(404, {"error": "not found"})

def _fake_post(url, json=None, files=None):
    if url.endswith('/path'):
        data = {
            "data": {
                "commands": ["RS00", "FW01", "SNAP1_A", "FIN"],
                "path": [
                    {"x": 1, "y": 1, "d": 0},
                    {"x": 2, "y": 1, "d": 0},
                ],
            }
        }
        return _FakeResponse(200, data)
    if url.endswith('/image'):
        payload = {"image_id": "1", "symbol": "A", "obstacle_id": 1}
        return _FakeResponse(200, payload)
    return _FakeResponse(404, {"error": "not found"})

wk8.requests.get = _fake_get
wk8.requests.post = _fake_post

# Fake picamera
class _FakePiCamera:
    def __init__(self):
        self.resolution = None
        self.framerate = None
        self.iso = None
        self.exposure_mode = None
        self.awb_mode = None
        self.exposure_compensation = None
        self.brightness = None
        self.contrast = None
        self.saturation = None
        self.sharpness = None
        self.shutter_speed = None
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False
    def capture(self, filename: str, format: str = 'jpeg', quality: int = 85):
        with open(filename, 'wb') as f:
            f.write(b"\xff\xd8\xff\xdb\x00C\x00" + b"\x00" * 64 + b"\xff\xd9")

sys.modules.setdefault('picamera', SimpleNamespace(PiCamera=_FakePiCamera))


class FakeAndroidLink:
    def __init__(self, incoming_queue: mp.Queue, outgoing_queue: mp.Queue):
        # incoming: messages (json strings) that recv() will return
        # outgoing: messages sent by android_sender via send()
        self._incoming = incoming_queue
        self._outgoing = outgoing_queue
    def connect(self):
        pass
    def disconnect(self):
        pass
    def send(self, message: AndroidMessage):
        self._outgoing.put({"cat": message.cat, "value": message.value})
    def recv(self):
        # Block for a bit, then raise OSError to simulate drop if empty
        try:
            return self._incoming.get(timeout=2.0)
        except Exception:
            # Simulate no message; return None to let loop continue
            return None


class FakeSTMLink:
    def __init__(self, incoming_queue: mp.Queue, sent_queue: mp.Queue):
        # incoming: ACK messages that recv() should return
        # sent_queue: capture commands the RPi sends to STM32
        self._incoming = incoming_queue
        self._sent = sent_queue
    def connect(self):
        pass
    def disconnect(self):
        pass
    def send(self, cmd: str):
        self._sent.put(cmd)
    def recv(self):
        return self._incoming.get()  # block until ACK available


def main():
    mgr = mp.Manager()
    android_in = mgr.Queue()   # Simulated messages from Android to RPi
    android_out = mgr.Queue()  # Messages sent from RPi to Android
    stm_in = mgr.Queue()       # Simulated ACKs coming from STM32 to RPi
    stm_sent = mgr.Queue()     # Commands RPi sends to STM32

    rpi = RaspberryPi()
    rpi.android_link = FakeAndroidLink(android_in, android_out)
    rpi.stm_link = FakeSTMLink(stm_in, stm_sent)

    # Start child processes (like start(), but without blocking reconnect loop)
    rpi.proc_recv_android = mp.Process(target=rpi.recv_android)
    rpi.proc_recv_stm32 = mp.Process(target=rpi.recv_stm)
    rpi.proc_android_sender = mp.Process(target=rpi.android_sender)
    rpi.proc_command_follower = mp.Process(target=rpi.command_follower)
    rpi.proc_rpi_action = mp.Process(target=rpi.rpi_action)

    for p in [rpi.proc_recv_android, rpi.proc_recv_stm32, rpi.proc_android_sender, rpi.proc_command_follower, rpi.proc_rpi_action]:
        p.start()

    try:
        # 1) Simulate obstacles being sent from Android
        obstacles_msg = json.dumps({
            "cat": "obstacles",
            "value": {
                "obstacles": [{"id": 1, "x": 2, "y": 1, "d": 0}],
                "mode": "0",
            },
        })
        android_in.put(obstacles_msg)

        # Give time for rpi_action to request algo and queue commands/path
        time.sleep(0.5)

        # 2) Simulate start command from Android
        start_msg = json.dumps({"cat": "control", "value": "start"})
        android_in.put(start_msg)

        # 3) Feed ACKs to move the command follower
        # First ACK corresponds to RS00 (gyro reset) and sets the rs_flag
        time.sleep(0.2)
        stm_in.put("ACK")

        # Second ACK will release the movement lock for the initial RS00 in the command queue
        time.sleep(0.2)
        stm_in.put("ACK")

        # Third ACK will acknowledge FW01 and advance location
        time.sleep(0.4)
        stm_in.put("ACK")

        # Allow time for SNAP flow (snap_and_rec releases lock internally) and FIN
        time.sleep(2.5)

        # Collect outputs
        sent_to_android = []
        while True:
            try:
                sent_to_android.append(android_out.get_nowait())
            except Exception:
                break

        sent_to_stm = []
        while True:
            try:
                sent_to_stm.append(stm_sent.get_nowait())
            except Exception:
                break

        print("Messages sent to Android (cat/value):")
        for m in sent_to_android:
            print(m)

        print("Commands sent to STM:")
        print(sent_to_stm)

        # Check internal shared states
        print("Current location dict:", dict(rpi.current_location))
        print("Success obstacles:", list(rpi.success_obstacles))
        print("Failed obstacles:", list(rpi.failed_obstacles))

    finally:
        # Cleanup processes
        for p in [rpi.proc_recv_android, rpi.proc_recv_stm32, rpi.proc_android_sender, rpi.proc_command_follower, rpi.proc_rpi_action]:
            if p.is_alive():
                p.terminate()
        for p in [rpi.proc_recv_android, rpi.proc_recv_stm32, rpi.proc_android_sender, rpi.proc_command_follower, rpi.proc_rpi_action]:
            p.join(timeout=2.0)


if __name__ == '__main__':
    main() 