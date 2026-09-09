"""Paced, full-duplex access to the device console.

The chip shell drops characters if a command is pasted at full speed, and a
naive reader misses output while it is writing. This writes a byte at a time
and drains continuously. Run it with the ESP-IDF Python environment.

    python3 scripts/console.py 'matter esp controller read-attr 1 0 29 3' --seconds 30
"""
import argparse
import time
import serial

p = argparse.ArgumentParser()
p.add_argument('commands', nargs='*')
p.add_argument('--seconds', type=float, default=10, help='how long to keep reading after the commands')
p.add_argument('--port', default='/dev/ttyACM0')
p.add_argument('--log', default='console.log')
a = p.parse_args()

with serial.Serial(a.port, 115200, timeout=.01) as s, open(a.log, 'ab') as log:
    def receive(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data = s.read(8192)
            if data:
                log.write(data)
                log.flush()
                print(data.decode(errors='replace'), end='', flush=True)

    for command in a.commands:
        for byte in (command + '\r').encode():
            s.write(bytes([byte]))
            receive(.005)
        receive(1)
    receive(a.seconds)
