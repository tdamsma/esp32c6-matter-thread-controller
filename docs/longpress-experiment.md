# Does the remote report a long press?

This document records the procedure and predictions written before testing,
followed by the measurements and conclusions.

## Summary

The remote does report a long press, on the endpoints where one is possible.
The three wheel-click endpoints (3, 6 and 9) advertise
`MomentarySwitchLongPress` and emit `LongPress` exactly 700 ms after
`InitialPress`, then `LongRelease` on release. The six wheel-turn endpoints do
not, which matches the hardware, since a detent step cannot be held. The
threshold is a hard edge: a press released at 700 ms or earlier is reported as
an ordinary tap.

The premise the experiment started from was wrong. `FeatureMap` was read on a
single endpoint and taken to describe the device. The sections below are kept in
the order they were written, with the predictions as they were registered.

## The question

The initial read of the IKEA BILRESA remote returned `FeatureMap = 22` from
one Switch cluster endpoint.

```
0x16 = MomentarySwitch (0x02) | MomentarySwitchRelease (0x04) | MomentarySwitchMultiPress (0x10)
```

`MomentarySwitchLongPress` is bit `0x08`, and `0x16 & 0x08 = 0`, so that endpoint
does not advertise `LongPress` (event 2) or `LongRelease` (event 4). The original
experiment incorrectly assumed this value applied to all nine endpoints.
The results below show that click endpoints report a different value.

The remote also reports `IdentifyType: None` and accepts no `TriggerEffect`,
but blinks when sent Identify. This prompted a check of observed behaviour
against the advertised features.

The experiment asked two questions.

1. Does the device emit `LongPress` and `LongRelease` anyway, unadvertised?
2. If it does not, can a hold still be recovered in software?
   `MomentarySwitchRelease` is set, and a single press is already known to
   arrive as `InitialPress` followed by `ShortRelease`. If `ShortRelease` is
   emitted when the button actually comes back up, then the gap between the two
   events is the hold duration, and long-press handling can live in the
   controller.

## Predictions

These predictions were written before testing and retained for comparison
with the results.

A. No `LongPress` or `LongRelease` at any hold duration. Confidence: high.
The `Identify` case was a device supporting a behaviour it failed to advertise,
whereas emitting these events would require the firmware to implement a hold
threshold it says it does not have.

B. `ShortRelease` arrives on release, so the measured gap tracks the hold.
Confidence: moderate. A wheel detent is a very short press, and a firmware built
around detent counting may well emit `InitialPress` and `ShortRelease` back to
back as one canned gesture, in which case the gap is constant and this
prediction is wrong.

C. A hold of several seconds produces no `MultiPress` events, because the
button never re-closes. Confidence: high.

D. A hold long enough to leave the device's 1000 ms active window still
delivers its release event promptly on arrival, because the subscription is
urgent, but the arrival time at the controller may lag by up to one idle poll
(about 7.5 s). This is why the analysis uses the device's own event timestamps
to avoid delays in event delivery.

## Why device timestamps

Every Matter event carries the timestamp the device recorded when it logged the
event (`EventHeader::mTimestamp`, uptime in milliseconds on this device).
Differences between those timestamps measure the gesture duration. Arrival
times also include radio and polling delays, which can last seconds while a
device is idle. `main/switch_watch.cpp` logs the device timestamp
on every Switch event for this reason.

## Procedure

1. Flash this repo's example and commission the remote, as in the README.
2. Set `kAcknowledgePress = false` in `main/switch_watch.cpp` and reflash. The
   acknowledgement sends `Identify` back on every press, which wakes the device
   mid-gesture and makes it blink.
3. Run the probe in prompted mode, which reads `FeatureMap` and `MultiPressMax`
   first and then walks through a tap, holds of 0.5, 1, 2, 5 and 10 seconds, and
   a double tap. Press ENTER, perform the gesture, wait for the capture window:

   ```sh
   cd example
   python3 scripts/longpress_probe.py --port /dev/ttyACM0 --buttons A,B,C --repeats 2
   ```

   Prompted mode needs an operator watching the terminal. Where that is not
   possible, freeform mode captures continuously and segments the stream into
   gestures afterwards, reading the capabilities at the end while the device is
   still awake:

   ```sh
   python3 scripts/longpress_probe.py --port /dev/ttyACM0 --freeform 240
   ```

4. Either mode writes a raw log and a report to `tmp/`. The analysis can be
   re-run on the raw log alone, without hardware:

   ```sh
   python3 scripts/longpress_probe.py --replay tmp/longpress-<stamp>.raw.log
   ```

Practical notes:

- Press a button before the capability read, or the sleepy device will not
  answer within the window.
- The wheel is a detent switch. Hold it pressed down rather than turned. A turn
  is reported as a multi-press and will be obvious in the table.
- Use two passes (`--repeats 2`) to check whether the measured gap is consistent
  for each hold duration.

## Reading the result

Compare the script’s verdict with the event table and raw log. The following
interpretations were proposed before testing.

| Observation | Conclusion |
|---|---|
| `LongPress` or `LongRelease` appear | Prediction A is wrong. Use the events directly and check the FeatureMap on the endpoint that emitted them. |
| Measured gap is close to the intended hold, across all durations and both passes | Software can detect a long press by starting a timer on `InitialPress` and checking it on `ShortRelease`. |
| Measured gap is constant (roughly 100 ms) whatever the hold | Prediction B is wrong. The event gap does not provide the hold duration. |
| No release event follows a held press | A hold is indistinguishable from a tap, and the press never completes as far as the device is concerned. |
| Gap varies but does not follow the hold | Inconclusive. Suspect the device's active window or a resubscription during the run, and check the raw log for gaps. |

If the gap tracks the hold, test shorter durations to determine whether
software can reliably distinguish a 500 ms hold from a tap.

## Results

Two runs on 2026-09-09. The first was performed by hand while the controller
logged events; the second used the probe's freeform mode. Both are reported
below.

The measurements showed that `FeatureMap` differs between turn and click
endpoints.

| Endpoints | FeatureMap | Features | MultiPressMax |
|---|---|---|---|
| 1, 2, 4, 5, 7, 8 (wheel turn) | 22 (`0x16`) | MS, MSR, MSM | 18 |
| 3, 6, 9 (wheel click) | 30 (`0x1E`) | MS, MSR, MSL, MSM | 3 |

The initial value of 22 came from a turn endpoint. The click endpoints
advertised `MomentarySwitchLongPress` in these measurements.

This split is corroborated on independent hardware. A Home Assistant
diagnostics dump of another BILRESA scroll wheel (firmware 1.8.7, spec
0x01030000), filed on
[home-assistant/core#159035](https://github.com/home-assistant/core/issues/159035),
reports the same FeatureMap 22 with `MultiPressMax` 18 on the six rotation
endpoints and FeatureMap 30 with `MultiPressMax` 3 on endpoints 3, 6 and 9.
Two units, same values, so this is a device property rather than a quirk of
the one remote measured here.

The first run gave three holds on endpoints 3 and 9, measured from the device's
own timestamps:

| InitialPress to LongPress | InitialPress to LongRelease |
|---|---|
| 700 ms | 1600 ms |
| 700 ms | 1980 ms |
| 700 ms | 6360 ms |

`LongPress` fires at a fixed 700 ms. `LongRelease` marks the actual release. A
held click emits no `ShortRelease` and no `MultiPressComplete`, so a hold and a
tap diverge at the second event.

### How the predictions did

- A (no long-press events at any duration): wrong. The initial
  FeatureMap reading came from a turn endpoint. Click endpoints advertise and
  emit long-press events.
- B (`ShortRelease` tracks the hold): not applicable. Held clicks
  emit `LongRelease` instead of `ShortRelease`.
- C (a hold produces no MultiPress events): correct. Held clicks produced
  `1, 2, 4` and nothing else.
- D (arrival times are unusable, device timestamps are needed): overstated
  for this gesture. The remote stays in its active window throughout a press,
  so across 94 consecutive same-endpoint event pairs the arrival-time gap
  differed from the device-time gap by at most 205 ms. Device
  timestamps gave exactly 700 ms three times, while the same gaps measured on
  arrival were 690, 580 and 750 ms.

### The scripted run

A second run through `scripts/longpress_probe.py --freeform`, walking the
gesture list twice on the three click endpoints, gave 87 events in 25 gestures
with the following results:

- `LongPress` onset was 700 ms in all six holds, on endpoints 3, 6 and 9.
  Combined with the earlier three, that is nine out of nine at exactly 700 ms.
- Presses released at 480, 560, 580, 620 and 700 ms were all reported as taps.
  Long-press events were observed only when the press outlasted 700 ms.
- The half-second hold, the gesture this run was designed around, is
  indistinguishable from a tap. Any long-press interaction has to be built on
  the device's own 700 ms boundary.
- The per-endpoint FeatureMap split was confirmed on all nine endpoints in the
  same capture.

Prompted mode is unusable when the operator cannot watch the terminal, which is
the case whenever the run is driven from a script or a remote session.
`--freeform` captures continuously and segments the stream into gestures
afterwards, keying on `InitialPress` to open a gesture and `LongRelease` or
`MultiPressComplete` to close it. That needs no synchronisation between the
gestures and the terminal. It was validated against the first run's capture
before being used.

### Incidental findings

- `MultiPressComplete` also follows a *single* press, carrying a count of 1, and
  its multi-press timer runs from the press rather than the release. Across
  sixteen single taps it arrived 500 to 520 ms after `InitialPress` when the
  button came up before then, and 15 ms after the release when the press
  outlasted that. Acting on `InitialPress` avoids the roughly half-second wait for the
  multi-press count.
- Commissioning a retail device needs the vendor's production PAA root, and
  without it the failure appears at the `AttestationRevocationCheck` step as
  `CHIP_ERROR_FAILED_DEVICE_ATTESTATION`, which does not point at the cause. See
  [../example/paa_cert/README.md](../example/paa_cert/README.md).
- `FeatureMap` is attribute `0xFFFC`. `0xFFFD` is `ClusterRevision`. Reading the
  wrong one returns a plausible small integer rather than an error.
- IKEA's Matter firmware has other gaps between what is declared and what is
  emitted. [connectedhomeip#73262](https://github.com/project-chip/connectedhomeip/issues/73262)
  (open) reports that a certified MYGGBETT contact sensor updates its
  `BooleanState` attribute but never emits the mandatory `StateChange` event,
  and cross-references a similar concern about this encoder. Which events a
  device actually sends is worth verifying on hardware rather than inferring
  from a FeatureMap or from certification, which is the same lesson this
  experiment's own wrong premise produced.

The device's own reference,
[ikea-bilresa-e2490](https://github.com/tdamsma/ikea-bilresa-e2490), carries
these results in its Switch cluster section.
