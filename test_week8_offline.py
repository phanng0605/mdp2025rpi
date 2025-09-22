#!/usr/bin/env python3
import json
import queue
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

# Inject a minimal stub for the 'bluetooth' module so importing Week_8 works without PyBluez
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

from Week_8 import RaspberryPi, AndroidMessage, PiAction


class FakeAndroidLink:
    def __init__(self):
        self.sent = []
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def send(self, message: AndroidMessage):
        # Collect messages for inspection
        self.sent.append({"cat": message.cat, "value": message.value})

    def recv(self):
        # Not used in this offline test; Week_8.start() is not called
        time.sleep(0.1)
        return None


class FakeSTMLink:
    def __init__(self):
        self.sent = []

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send(self, cmd: str):
        self.sent.append(cmd)

    def recv(self):
        # Not used here; no command_follower loop in this test
        return ""


class FakePiCamera:
    def __init__(self):
        # Expose attributes used by Week_8
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
        # Write a minimal JPEG header so file exists
        with open(filename, 'wb') as f:
            f.write(b"\xff\xd8\xff\xdb\x00C\x00" + b"\x00" * 64 + b"\xff\xd9")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode("utf-8")


def main():
    # Inject fake picamera module
    fake_picamera_module = SimpleNamespace(PiCamera=FakePiCamera)
    sys.modules['picamera'] = fake_picamera_module

    # Instantiate RaspberryPi and replace links with fakes
    rpi = RaspberryPi()
    rpi.android_link = FakeAndroidLink()
    rpi.stm_link = FakeSTMLink()

    # Prepare mock HTTP behavior
    def fake_get(url, timeout=None):
        if url.endswith('/status'):
            return FakeResponse(200, {"ok": True})
        if url.endswith('/stitch'):
            return FakeResponse(200, {"ok": True})
        return FakeResponse(404, {"error": "not found"})

    def fake_post(url, json=None, files=None):
        if url.endswith('/path'):
            # Provide a tiny plan including a SNAP and FIN
            data = {
                "data": {
                    "commands": ["FW01", "SNAP1_A", "FIN"],
                    "path": [
                        {"x": 1, "y": 1, "d": 0},
                        {"x": 2, "y": 1, "d": 0},
                    ],
                }
            }
            return FakeResponse(200, data)
        if url.endswith('/image'):
            # Return a successful recognition on first try
            payload = {
                "image_id": "1",
                "symbol": "A",
                "obstacle_id": 1,
            }
            return FakeResponse(200, payload)
        return FakeResponse(404, {"error": "not found"})

    # Run test flow under patched requests
    with patch('requests.get', side_effect=fake_get), patch('requests.post', side_effect=fake_post):
        # Simulate setting obstacles via direct call
        obstacles = {
            "obstacles": [
                {"id": 1, "x": 2, "y": 1, "d": 0},
            ],
            "mode": "0",
        }
        # In production this comes from rpi_action queue, call method directly for test
        for obs in obstacles['obstacles']:
            rpi.obstacles[obs['id']] = obs

        rpi.request_algo(obstacles)
        print("Commands queued:")
        cmds = []
        try:
            while True:
                cmds.append(rpi.command_queue.get_nowait())
        except queue.Empty:
            pass
        print(cmds)

        print("Path queued:")
        path = []
        try:
            while True:
                path.append(rpi.path_queue.get_nowait())
        except queue.Empty:
            pass
        print(path)

        # Put commands back for visibility if desired
        for c in cmds:
            rpi.command_queue.put(c)
        for p in path:
            rpi.path_queue.put(p)

        # Test snap_and_rec directly (no movement loop)
        # Ensure movement lock is acquired as snap_and_rec will release it
        rpi.movement_lock.acquire()
        rpi.snap_and_rec("1_A")

        # Drain Android messages and print
        android_msgs = []
        try:
            while True:
                msg = rpi.android_queue.get_nowait()
                android_msgs.append({"cat": msg.cat, "value": msg.value})
        except queue.Empty:
            pass

        print("Android messages:")
        for m in android_msgs:
            print(m)

        print("Success obstacles:", list(rpi.success_obstacles))
        print("Failed obstacles:", list(rpi.failed_obstacles))

        # Test stitching
        rpi.request_stitch()
        # Drain any new Android messages
        try:
            while True:
                msg = rpi.android_queue.get_nowait()
                print({"cat": msg.cat, "value": msg.value})
        except queue.Empty:
            pass


if __name__ == '__main__':
    main()
