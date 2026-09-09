"""Commission a device over BLE and Thread, without printing its secrets.

The setup passcode and the Thread dataset (which contains the network key) are
device secrets. This reads the setup code from a file, reads the dataset off the
controller itself, and redacts both from everything it prints.

    echo 'MT:XXXXXXXXXXXXXXXXXXX' > tmp/setup_code.txt      # or the 11-digit code
    python3 scripts/pair.py --port /dev/tty.usbmodem1101 --code-file ../tmp/setup_code.txt

Run it with the ESP-IDF Python environment. The device must be in its Matter
pairing mode and the controller's Thread network must already be up.
"""
import argparse
import re
import sys
import time

BASE38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."
CHUNK_BYTES = {5: 3, 4: 2, 2: 1}


def base38_decode(text):
    """Inverse of the encoder in the Matter SDK: little-endian chunks."""
    out = bytearray()
    for start in range(0, len(text), 5):
        chunk = text[start:start + 5]
        if len(chunk) not in CHUNK_BYTES:
            raise ValueError("bad base38 chunk length %d" % len(chunk))
        value = 0
        for position, character in enumerate(chunk):
            if character not in BASE38:
                raise ValueError("character %r is not base38" % character)
            value += BASE38.index(character) * 38 ** position
        out += value.to_bytes(CHUNK_BYTES[len(chunk)], "little")
    return bytes(out)


def parse_setup_code(code):
    """Return (passcode, discriminator) from a QR payload or a manual code.

    QR carries the full 12-bit discriminator. The 11-digit manual code carries
    only the 4-bit short discriminator, which BLE pairing cannot use, so that
    form needs --discriminator as well.
    """
    code = code.strip().replace(" ", "").replace("-", "") if not code.startswith("MT:") else code.strip()
    if code.startswith("MT:"):
        bits = int.from_bytes(base38_decode(code[3:]), "little")
        # Field order from the LSB up: version, vid, pid, flow, discovery,
        # discriminator, passcode.
        discriminator = (bits >> 45) & 0xFFF
        passcode = (bits >> 57) & 0x7FFFFFF
        return passcode, discriminator
    if not code.isdigit() or len(code) not in (11, 21):
        raise ValueError("not a QR payload or an 11/21-digit manual code")
    first = int(code[0:1])
    chunk = int(code[1:6])
    passcode = (int(code[6:10]) << 14) | (chunk & 0x3FFF)
    short_discriminator = ((first & 0x3) << 2) | ((chunk >> 14) & 0x3)
    return passcode, -short_discriminator - 1  # negative marks "short only"


class Console:
    def __init__(self, port, secrets):
        import serial

        self.serial = serial.Serial(port, 115200, timeout=0.01)
        self.secrets = secrets
        self.text = ""

    def redact(self, text):
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def receive(self, seconds, quiet=False):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data = self.serial.read(8192)
            if not data:
                continue
            text = data.decode(errors="replace")
            self.text += text
            if not quiet:
                print(self.redact(text), end="", flush=True)

    def send(self, command, quiet=False):
        for byte in (command + "\r").encode():
            self.serial.write(bytes([byte]))
            self.receive(0.005, quiet=True)
        self.receive(1, quiet=quiet)


def read_dataset(console):
    """Ask the controller for its active operational dataset, in hex."""
    before = len(console.text)
    console.send("matter esp ot_cli dataset active -x", quiet=True)
    console.receive(2, quiet=True)
    for line in console.text[before:].splitlines():
        candidate = line.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40,}", candidate):
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--node", type=int, default=1, help="node id to give the device")
    parser.add_argument("--code-file", default="../tmp/setup_code.txt", help="file holding the setup code")
    parser.add_argument("--discriminator", type=int, help="12-bit discriminator, needed with a manual code")
    parser.add_argument("--seconds", type=float, default=180, help="how long to watch for the result")
    arguments = parser.parse_args()

    with open(arguments.code_file, encoding="utf-8") as handle:
        passcode, discriminator = parse_setup_code(handle.read())
    if discriminator < 0:
        if arguments.discriminator is None:
            print("The manual code only carries the 4-bit short discriminator (%d). "
                  "Re-run with --discriminator <12-bit value>, or use the MT: QR payload."
                  % (-discriminator - 1))
            return 2
        discriminator = arguments.discriminator

    console = Console(arguments.port, [str(passcode)])
    dataset = read_dataset(console)
    if not dataset:
        print("No active Thread dataset on the controller. Bring the network up first.")
        return 1
    console.secrets.append(dataset)
    print("Dataset and passcode read, both redacted from here on. Discriminator %d." % discriminator)

    console.send("matter esp controller pairing ble-thread %d %s %d %d"
                 % (arguments.node, dataset, passcode, discriminator))

    end = time.monotonic() + arguments.seconds
    while time.monotonic() < end:
        console.receive(2)
        if "Commissioning success" in console.text:
            print("\nCommissioned as node %d." % arguments.node)
            return 0
        if "Commissioning failure" in console.text:
            print("\nCommissioning failed. See the log above.")
            return 1
    print("\nNo result within %.0f s." % arguments.seconds)
    return 1


if __name__ == "__main__":
    sys.exit(main())
