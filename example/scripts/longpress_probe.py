"""Find out how a Matter switch reports a long press.

A device that does not advertise MomentarySwitchLongPress (FeatureMap bit 0x08)
should not emit LongPress or LongRelease. If it advertises
MomentarySwitchRelease (0x04) it still emits ShortRelease when the button comes
back up, so the gap between InitialPress and ShortRelease is the hold duration.
Two things are therefore worth establishing on hardware:

  1. whether the device emits LongPress (2) and LongRelease (4) anyway, the way
     it blinks on Identify while reporting IdentifyType None (README, point 5)
  2. if not, whether ShortRelease arrives late enough, and consistently enough,
     to recover the hold duration in software

This drives the controller console through a scripted set of gestures, captures
the Switch events the firmware logs, and answers both from the device's own
event timestamps rather than from arrival times here, which a sleepy device's
poll cadence stretches by seconds.

Needs the switch_watch build in this repo flashed on the controller, the device
already commissioned, and pyserial. Run it with the ESP-IDF Python environment.

    python3 scripts/longpress_probe.py --port /dev/ttyACM0 --buttons A,B,C

Analysis only, against a log a previous run wrote (no hardware needed):

    python3 scripts/longpress_probe.py --replay tmp/longpress-20260909-101500.raw.log
"""
import argparse
import datetime
import os
import re
import statistics
import sys
import time

SWITCH_CLUSTER = 59
FEATURE_MAP_ATTR = 65532  # 0xFFFC; 0xFFFD is ClusterRevision
MULTI_PRESS_MAX_ATTR = 2

FEATURES = [
    (0x01, "LatchingSwitch"),
    (0x02, "MomentarySwitch"),
    (0x04, "MomentarySwitchRelease"),
    (0x08, "MomentarySwitchLongPress"),
    (0x10, "MomentarySwitchMultiPress"),
    (0x20, "ActionSwitch"),
]

EVENTS = {
    0: "SwitchLatched",
    1: "InitialPress",
    2: "LongPress",
    3: "ShortRelease",
    4: "LongRelease",
    5: "MultiPressOngoing",
    6: "MultiPressComplete",
}

INITIAL_PRESS, LONG_PRESS, SHORT_RELEASE, LONG_RELEASE = 1, 2, 3, 4
MULTI_PRESS_COMPLETE = 6

# switch_watch: event ep=1 id=3 num=1234 t=456789ms_uptime fields=[0=0] history
EVENT_LINE = re.compile(
    r"switch_watch: event ep=(\d+) id=(\d+) num=(\d+) t=(\d+)(ms_uptime|us_epoch) "
    r"fields=\[([^\]]*)\](?P<history> history)?"
)
TRIAL_MARKER = re.compile(r"#### trial (\d+) button=(\S+) gesture=(\S+) hold_ms=(\d+)")

# The console's attribute logger prints the path, then the value on its own line:
#   Endpoint: 1 Cluster: 0x0000_003B Attribute 0x0000_FFFD DataVersion: 7
#     FeatureMap: 22
ATTRIBUTE_PATH = re.compile(r"Endpoint: (\d+) Cluster: 0x0000_003B Attribute 0x0000_[0-9A-F]{4}")
ATTRIBUTE_VALUE = re.compile(r"\b(FeatureMap|MultiPressMax): (\d+)\b")

# name, hold in ms, taps, what it is for.
GESTURES = [
    ("tap", 0, 1, "baseline: the shortest press you can make"),
    ("hold_500ms", 500, 1, "brackets the long-press threshold from below"),
    ("hold_1s", 1000, 1, "just past a typical long-press threshold"),
    ("hold_2s", 2000, 1, "a deliberate hold"),
    ("hold_5s", 5000, 1, "long enough that the device leaves its active window"),
    ("hold_10s", 10000, 1, "long enough for a repeat or a pairing gesture to trigger"),
    ("double_tap", 0, 2, "for comparison: MultiPress against a hold"),
]


def parse_features(value):
    known = [name for bit, name in FEATURES if value & bit]
    unknown = value & ~sum(bit for bit, _ in FEATURES)
    if unknown:
        known.append("unknown bits 0x%02x" % unknown)
    return known


class Disconnected(Exception):
    """The serial port went away mid-capture."""


class Console:
    """Paced, full-duplex access to the device console (see console.py)."""

    def __init__(self, port, log):
        import serial

        self.serial = serial.Serial(port, 115200, timeout=0.01)
        self.log = log
        self.buffer = []

    def receive(self, seconds):
        """Drain the port for a while. A USB-CDC port vanishes when the board
        resets, so a read failure ends the capture instead of killing the run."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                data = self.serial.read(8192)
            except OSError as failure:  # serial.SerialException derives from OSError
                raise Disconnected(str(failure)) from failure
            if data:
                text = data.decode(errors="replace")
                self.buffer.append(text)
                self.log.write(text)
                self.log.flush()
                print(text, end="", flush=True)

    def send(self, command):
        for byte in (command + "\r").encode():
            self.serial.write(bytes([byte]))
            self.receive(0.005)
        self.receive(1)

    def note(self, line):
        self.log.write(line + "\n")
        self.log.flush()
        self.buffer.append(line + "\n")


def read_capabilities(console, node, seconds):
    """Read FeatureMap and MultiPressMax on every endpoint's Switch cluster.

    The device is sleepy, so this only lands promptly while it is awake. Values
    are echoed by the console's own attribute logger.
    """
    for attribute in (FEATURE_MAP_ATTR, MULTI_PRESS_MAX_ATTR):
        console.send("matter esp controller read-attr %d 0xffff %d %d" % (node, SWITCH_CLUSTER, attribute))
        console.receive(seconds)


def run_trials(console, buttons, gestures, repeats, settle):
    trial = 0
    for repeat in range(repeats):
        for button in buttons:
            for name, hold_ms, taps, purpose in gestures:
                trial += 1
                hold = "%.1f s" % (hold_ms / 1000.0) if hold_ms else "as briefly as you can"
                action = "tap %d times" % taps if taps > 1 else "press and hold for %s" % hold
                print("\n=== trial %d (pass %d) button %s: %s (%s)" % (trial, repeat + 1, button, name, purpose))
                print("    %s, then release. Press ENTER when you are ready." % action)
                input()
                console.note("#### trial %d button=%s gesture=%s hold_ms=%d" % (trial, button, name, hold_ms))
                print("    go, capturing for %.0f s" % settle)
                console.receive(settle)


def parse_capabilities(text):
    """Pull the Switch cluster's FeatureMap and MultiPressMax out of a capture."""
    capabilities = {}
    endpoint = None
    for line in text.splitlines():
        path = ATTRIBUTE_PATH.search(line)
        if path:
            endpoint = int(path.group(1))
            continue
        value = ATTRIBUTE_VALUE.search(line)
        if value and endpoint is not None:
            capabilities.setdefault(endpoint, {})[value.group(1)] = int(value.group(2))
    return capabilities


def parse_events(text):
    """Every live Switch event in a capture, in device order."""
    events = []
    for line in text.splitlines():
        match = EVENT_LINE.search(line)
        if not match or match.group("history"):
            continue
        milliseconds = int(match.group(4))
        if match.group(5) == "us_epoch":
            milliseconds //= 1000
        events.append({
            "endpoint": int(match.group(1)),
            "id": int(match.group(2)),
            "number": int(match.group(3)),
            "ms": milliseconds,
            "fields": match.group(6),
        })
    events.sort(key=lambda e: e["number"])
    return events


def segment(events):
    """Group a free-running event stream into gestures.

    A gesture starts at InitialPress on an endpoint and ends at the event that
    closes it: LongRelease for a hold, MultiPressComplete for taps. Endpoints
    interleave freely, so each is tracked on its own.
    """
    open_gestures = {}
    done = []
    for event in events:
        endpoint = event["endpoint"]
        if endpoint not in open_gestures:
            if event["id"] != INITIAL_PRESS:
                continue
            open_gestures[endpoint] = {"endpoint": endpoint, "events": []}
        open_gestures[endpoint]["events"].append(event)
        if event["id"] in (LONG_RELEASE, MULTI_PRESS_COMPLETE):
            done.append(open_gestures.pop(endpoint))
    done.extend(open_gestures.values())
    done.sort(key=lambda g: g["events"][0]["number"])
    return done


def describe(gesture):
    """One row: what the device reported, and the timings that matter."""
    events = gesture["events"]
    start = events[0]["ms"]
    ids = [e["id"] for e in events]
    onset = next((e["ms"] - start for e in events if e["id"] == LONG_PRESS), None)
    release = next((e["ms"] - start for e in events if e["id"] in (SHORT_RELEASE, LONG_RELEASE)), None)
    end = next((e["ms"] - start for e in events if e["id"] == LONG_RELEASE), None)
    complete = next((e for e in events if e["id"] == MULTI_PRESS_COMPLETE), None)
    count = complete["fields"].split(",")[-1].split("=")[-1] if complete else None
    if LONG_PRESS in ids:
        kind = "hold"
    elif count:
        kind = "tap x%s" % count
    else:
        kind = "incomplete"
    return {
        "endpoint": gesture["endpoint"],
        "kind": kind,
        "onset": onset,
        "held": end if end is not None else release,
        "complete": (complete["ms"] - start) if complete else None,
        "sequence": " ".join(EVENTS.get(i, "id%d" % i) for i in ids),
    }


def freeform_report(text):
    events = parse_events(text)
    gestures = segment(events)
    lines = ["| # | endpoint | reported as | to LongPress | press to release | to MultiPressComplete | sequence |",
             "|---|---|---|---|---|---|---|"]
    for index, gesture in enumerate(gestures, 1):
        row = describe(gesture)
        lines.append("| %d | %d | %s | %s | %s | %s | %s |" % (
            index, row["endpoint"], row["kind"],
            "%d ms" % row["onset"] if row["onset"] is not None else "-",
            "%d ms" % row["held"] if row["held"] is not None else "-",
            "%d ms" % row["complete"] if row["complete"] is not None else "-",
            row["sequence"]))
    onsets = [describe(g)["onset"] for g in gestures]
    onsets = [o for o in onsets if o is not None]
    lines.append("")
    if onsets:
        lines.append("**LongPress onset:** %d measurements, %d to %d ms." % (len(onsets), min(onsets), max(onsets)))
    else:
        lines.append("**LongPress onset:** no LongPress in this capture.")
    return "\n".join(lines), len(events), len(gestures)


def parse(text):
    """Split a prompted capture into trials of (endpoint, event id, number, ms, fields)."""
    trials = []
    current = None
    for line in text.splitlines():
        marker = TRIAL_MARKER.search(line)
        if marker:
            current = {
                "trial": int(marker.group(1)),
                "button": marker.group(2),
                "gesture": marker.group(3),
                "hold_ms": int(marker.group(4)),
                "events": [],
            }
            trials.append(current)
            continue
        match = EVENT_LINE.search(line)
        if not match or current is None or match.group("history"):
            continue
        milliseconds = int(match.group(4))
        if match.group(5) == "us_epoch":
            milliseconds //= 1000
        current["events"].append(
            {
                "endpoint": int(match.group(1)),
                "id": int(match.group(2)),
                "number": int(match.group(3)),
                "ms": milliseconds,
                "fields": match.group(6),
            }
        )
    return trials


def measure(trial):
    """Device-side gap from InitialPress to the release that closed it."""
    press = None
    for event in sorted(trial["events"], key=lambda e: e["number"]):
        if event["id"] == INITIAL_PRESS:
            press = event
        elif event["id"] in (SHORT_RELEASE, LONG_RELEASE) and press is not None:
            if event["endpoint"] == press["endpoint"]:
                return event["ms"] - press["ms"], EVENTS[event["id"]]
    return None, None


def long_press_delay(trial):
    """Device-side gap from InitialPress to LongPress, where the device sends one."""
    press = None
    for event in sorted(trial["events"], key=lambda e: e["number"]):
        if event["id"] == INITIAL_PRESS:
            press = event
        elif event["id"] == LONG_PRESS and press is not None and event["endpoint"] == press["endpoint"]:
            return event["ms"] - press["ms"]
    return None


def verdict(trials):
    holds = [t for t in trials if t["hold_ms"] > 0]
    long_events = sorted({e["id"] for t in trials for e in t["events"]} & {LONG_PRESS, LONG_RELEASE})
    if long_events:
        onsets = [d for d in (long_press_delay(t) for t in trials) if d is not None]
        threshold = ""
        if onsets:
            threshold = " LongPress fires %d to %d ms after the press." % (min(onsets), max(onsets))
        return ("The device emits %s despite not advertising MomentarySwitchLongPress. Use them.%s"
                % (", ".join(EVENTS[i] for i in long_events), threshold))
    measured = [(t["hold_ms"], measure(t)[0]) for t in holds]
    measured = [(intended, got) for intended, got in measured if got is not None]
    if not measured:
        if not any(e["id"] == INITIAL_PRESS for t in trials for e in t["events"]):
            return "No events captured at all, so the subscription was not live. Nothing can be concluded."
        return "No release event ever followed a press, so a hold is indistinguishable from a tap."
    errors = [got - intended for intended, got in measured]
    spread = max(got for _, got in measured) - min(got for _, got in measured)
    if all(abs(error) < 0.3 * intended + 250 for (intended, _), error in zip(measured, errors)):
        return (
            "No LongPress or LongRelease, but the release tracks the hold to within "
            "%d ms across %.1f-%.1f s. Detect long presses in software by timing "
            "InitialPress to ShortRelease." % (max(abs(e) for e in errors), min(i for i, _ in measured) / 1000.0,
                                               max(i for i, _ in measured) / 1000.0)
        )
    if spread < 250:
        return (
            "The release always lands ~%d ms after the press regardless of how "
            "long the button was held, so the device reports a fixed gesture and "
            "the hold is not recoverable." % statistics.median(got for _, got in measured)
        )
    return "Release timing varies but does not follow the hold; inspect the table before concluding."


def report(trials, capabilities):
    lines = []
    if capabilities:
        lines += ["| endpoint | FeatureMap | features | MultiPressMax |", "|---|---|---|---|"]
        for endpoint in sorted(capabilities):
            values = capabilities[endpoint]
            feature_map = values.get("FeatureMap")
            lines.append(
                "| %d | %s | %s | %s |"
                % (
                    endpoint,
                    "%d (0x%02x)" % (feature_map, feature_map) if feature_map is not None else "-",
                    ", ".join(parse_features(feature_map)) if feature_map is not None else "-",
                    values.get("MultiPressMax", "-"),
                )
            )
        lines.append("")
    lines += ["| trial | button | gesture | intended | measured | to LongPress | closed by | events |",
             "|---|---|---|---|---|---|---|---|"]
    for trial in trials:
        gap, closer = measure(trial)
        onset = long_press_delay(trial)
        names = " ".join("%s@ep%d" % (EVENTS.get(e["id"], "id%d" % e["id"]), e["endpoint"]) for e in trial["events"])
        lines.append(
            "| %d | %s | %s | %s | %s | %s | %s | %s |"
            % (
                trial["trial"],
                trial["button"],
                trial["gesture"],
                "%d ms" % trial["hold_ms"] if trial["hold_ms"] else "-",
                "%d ms" % gap if gap is not None else "-",
                "%d ms" % onset if onset is not None else "-",
                closer or "-",
                names or "(none)",
            )
        )
    lines += ["", "**Verdict:** " + verdict(trials)]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--node", type=int, default=1, help="node id the device was paired as")
    parser.add_argument("--buttons", default="A", help="comma-separated labels for the buttons to test")
    parser.add_argument("--repeats", type=int, default=1, help="passes over the whole gesture set")
    parser.add_argument("--settle", type=float, default=12, help="seconds to keep capturing after each gesture")
    parser.add_argument("--out", default="tmp", help="directory for the raw log and the report")
    parser.add_argument("--replay", help="analyse a raw log from a previous run instead of talking to hardware")
    parser.add_argument("--freeform", type=float,
                        help="capture for this many seconds with no prompts, then segment the stream "
                             "into gestures by itself. Use when the operator cannot watch the prompts.")
    arguments = parser.parse_args()

    if arguments.freeform:
        os.makedirs(arguments.out, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        raw_path = os.path.join(arguments.out, "longpress-%s.raw.log" % stamp)
        report_path = os.path.join(arguments.out, "longpress-%s.md" % stamp)
        with open(raw_path, "w", encoding="utf-8") as raw:
            console = Console(arguments.port, raw)
            try:
                console.receive(arguments.freeform)
                # Read the capabilities last, while the device is still awake
                # from the gestures.
                read_capabilities(console, arguments.node, arguments.settle)
            except (Disconnected, KeyboardInterrupt):
                pass
            text = "".join(console.buffer)
        body, event_count, gesture_count = freeform_report(text)
        capabilities = parse_capabilities(text)
        if capabilities:
            head = ["| endpoint | FeatureMap | features | MultiPressMax |", "|---|---|---|---|"]
            for endpoint in sorted(capabilities):
                values = capabilities[endpoint]
                feature_map = values.get("FeatureMap")
                head.append("| %d | %s | %s | %s |" % (
                    endpoint,
                    "%d (0x%02x)" % (feature_map, feature_map) if feature_map is not None else "-",
                    ", ".join(parse_features(feature_map)) if feature_map is not None else "-",
                    values.get("MultiPressMax", "-")))
            body = "\n".join(head) + "\n\n" + body
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write("# Long-press probe %s\n\n%s\n" % (stamp, body))
        print(body)
        print("\n%d events, %d gestures.\nraw log: %s\nreport:  %s"
              % (event_count, gesture_count, raw_path, report_path))
        return 0

    if arguments.replay:
        with open(arguments.replay, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        trials = parse(text)
        print(report(trials, parse_capabilities(text)))
        return 0 if trials else 1

    os.makedirs(arguments.out, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_path = os.path.join(arguments.out, "longpress-%s.raw.log" % stamp)
    report_path = os.path.join(arguments.out, "longpress-%s.md" % stamp)

    interrupted = None
    with open(raw_path, "w", encoding="utf-8") as raw:
        console = Console(arguments.port, raw)
        try:
            print("Draining the console. Press any button on the device to wake it, then ENTER.")
            console.receive(2)
            input()
            console.receive(3)
            read_capabilities(console, arguments.node, arguments.settle)
            run_trials(console, arguments.buttons.split(","), GESTURES, arguments.repeats, arguments.settle)
        except (Disconnected, KeyboardInterrupt) as stop:
            # Report on what was captured rather than losing the whole run.
            interrupted = stop
        text = "".join(console.buffer)

    trials = parse(text)
    body = report(trials, parse_capabilities(text))
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write("# Long-press probe %s\n\n%s\n" % (stamp, body))
    print("\n" + body)
    if interrupted is not None:
        print("\nRun ended early (%s). The table covers the trials that completed."
              % (interrupted.__class__.__name__ if not str(interrupted) else interrupted))
    print("\nraw log: %s\nreport:  %s" % (raw_path, report_path))
    return 1 if interrupted is not None else 0


if __name__ == "__main__":
    sys.exit(main())
