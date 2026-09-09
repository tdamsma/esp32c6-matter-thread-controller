# ESP32-C6 as a standalone Matter controller over Thread

This firmware runs a Matter controller on an ESP32-C6. It leads its own Thread
network, runs the SRP server and DNS-SD responder itself, commissions a device
over BLE, and subscribes to device events. It uses 4 MB of flash and runs
without a border router, hub, PSRAM or Wi-Fi.

This project adapts Espressif's controller example, which ships S3 sdkconfigs
and defaults to an 8 MB flash with a dual-OTA layout, to a 4 MB ESP32-C6.

The controller was tested on an M5Stack NanoC6 with an IKEA BILRESA remote.
Device measurements are documented in
[ikea-bilresa-e2490](https://github.com/tdamsma/ikea-bilresa-e2490).

This document was drafted with Claude and OpenAI models. Hardware measurements
and experiment results are documented below and in the linked experiment notes.
I’m sharing this in the hope that it’s useful to others.

## Features

- Thread network with the ESP32-C6 as leader
- SRP server + DNS-SD on the same node, so a commissioned device is resolvable
- BLE commissioning of a retail device, including production attestation
- Urgent event subscriptions (see [event delivery](#1-urgent-event-subscriptions))
- An interactive console for pairing, reads, writes, invokes and subscriptions

## Measured footprint

Measured for the example in this repo, built for the ESP32-C6 with ESP-IDF
6.0.2:

| | |
|---|---|
| Application binary | 2,782,144 bytes |
| App partition | 3,932,160 bytes (single factory app, 4 MB flash) |
| Free in the app partition | 1,150,016 bytes (29 %) |
| Free internal heap after startup | ~150 KB |
| Minimum-ever free heap through commissioning | ~137 KB |
| PSRAM | Not used |

## Requirements

- ESP32-C6 board with 4 MB flash (using the native radio and USB serial)
- ESP-IDF v6.0.2
- esp-matter checked out, with `ESP_MATTER_PATH` exported and its `export.sh` sourced
  (the build needs `gn` on PATH, which that provides)

## Build and flash

```sh
. $IDF_PATH/export.sh
export ESP_MATTER_PATH=$HOME/esp/esp-matter
. $ESP_MATTER_PATH/export.sh

cd example
idf.py set-target esp32c6
idf.py build flash monitor
```

Before commissioning a retail device, put its manufacturer's production PAA
root into `example/paa_cert/`. The root ships with the esp-matter checkout you
used for the build. See [paa_cert/README.md](example/paa_cert/README.md) for
copying and naming instructions.
Without it the example's trust store holds only the Matter test roots, and
commissioning fails at the attestation steps with
`CHIP_ERROR_FAILED_DEVICE_ATTESTATION`.

## Commissioning a device

The device's setup passcode and discriminator come from its Matter setup
code, printed as a QR code or an 11-digit manual pairing code on the device
and its paperwork. Decode either with `chip-tool`:

```sh
chip-tool payload parse-setup-payload MT:XXXXXXXXXXXXXXXXXXX
chip-tool payload parse-manual-code 1234-567-8901
```

Keep these values private. Do not paste them into issues or search engines.

On the controller's console, bring up the Thread network, read its operational
dataset, and pair.

```
> matter esp ot_cli dataset init new
> matter esp ot_cli dataset commit active
> matter esp ot_cli ifconfig up
> matter esp ot_cli thread start
> matter esp ot_cli dataset active -x      # prints the dataset TLV hex
> matter esp controller pairing ble-thread <node-id> <dataset-hex> <passcode> <discriminator>
```

Put the device into its own Matter pairing mode first. On success you get
`Commissioning success with node ...`, and the device shows up as a Thread child.

`example/scripts/pair.py` does the same from a host. It reads the setup code
from a file and the Thread dataset from the controller, and redacts both the
passcode and the dataset from everything it prints, so neither ends up in a
terminal log:

```sh
python3 scripts/pair.py --port /dev/ttyACM0 --node 1 --code-file ../tmp/setup_code.txt
```

After commissioning, use these commands to read attributes and events, send
commands, and subscribe to events.

```
> matter esp controller read-attr   <node-id> <endpoints> <clusters> <attributes>
> matter esp controller read-event  <node-id> 0xffff <cluster> 0xffffffff
> matter esp controller invoke-cmd  <node-id> <endpoint> <cluster> <command> '{"0:U16":3}'
> matter esp controller subs-event  <node-id> <endpoints> <clusters> <events> <min> <max>
```

`example/scripts/console.py` drives the console from a host. It writes one byte
at a time and reads output concurrently to avoid the lost replies observed
with `cat` and `screen`.

`example/scripts/longpress_probe.py` uses it to capture press-and-hold events.
The procedure and results are documented in
[docs/longpress-experiment.md](docs/longpress-experiment.md).

## Implementation notes

### 1. Urgent event subscriptions

A Matter event path carries an `IsUrgent` flag. Without it a server may hold
events until the next scheduled report. A battery-powered device negotiates that
interval to match its idle mode. The remote tested here negotiated 900 seconds
when the controller requested 60 seconds.

Without urgency, the subscription established and reads succeeded, but no event
reports arrived during testing. The recorded events could still be retrieved
with `read-event`, and no errors were reported.

esp-matter's single-path `subscribe_command` constructor cannot set
the flag. Build the paths by hand instead:

```cpp
ScopedMemoryBufferWithSize<EventPathParams> events;
events.Alloc(1);
events[0] = EventPathParams(chip::kInvalidEndpointId, kSwitchCluster,
                            chip::kInvalidEventId, true /* urgent */);
```

With the flag set, events arrived in milliseconds. Home Assistant's controller
sets urgency internally. Custom controllers need to set it explicitly.

### 2. SRP and DNS-SD configuration

Commissioning completes and then the controller cannot reach the node it just
paired, because nothing on the network resolves Thread service records.

Enable OpenThread's SRP server and DNS-SD server at build time, turn the SRP
server on at runtime, and use platform DNS-SD. Minimal mDNS cannot resolve a
Thread-only node without a border router.

```
-DOPENTHREAD_CONFIG_SRP_SERVER_ENABLE=1 -DOPENTHREAD_CONFIG_DNSSD_SERVER_ENABLE=1
CONFIG_USE_MINIMAL_MDNS=n
otSrpServerSetEnabled(esp_openthread_get_instance(), true);
```

After the controller reboots, its SRP registry is empty until the
device re-registers, so the first attempt to reach a known device can fail with
`operational discovery failed: 32` (timeout). The example retries every 30 s.

### 3. Native Thread radio configuration

`esp_openthread_platform_config_t` is only set in the example's border-router
path. A plain Thread build starts with no radio configuration and panics. Set
`RADIO_MODE_NATIVE`, `HOST_CONNECTION_MODE_NONE` and an NVS-backed port config
unconditionally, as shown in `example/main/app_main.cpp`.

### 4. Command latency while idle

An intermittently connected device (ICD) polls its parent slowly when idle and
quickly during a short active window after a button press. The device tested
here reports:

```
IdleModeDuration   900 s
ActiveModeDuration 1000 ms
```
and polls every ~7.5 s idle versus ~100 ms active.

A command sent while the device is idle may wait seconds. A command sent in
response to an event arrives almost immediately. To acknowledge a button press
with an LED blink, send Identify from the event handler.

The parent's logs can be used to observe the polling interval.

### 5. Identify response

The remote tested here reports `IdentifyType: 0` (None) and accepts no
`TriggerEffect` command. Sending Identify blinks its LEDs despite the reported
`IdentifyType`.

## The example

The firmware and host scripts are in `example/`.

| File | Purpose |
|---|---|
| `main/app_main.cpp` | Thread platform config, Matter start, SRP server, commissioner |
| `main/switch_watch.cpp` | Urgent event subscription, decoding, debounce, optional acknowledgement |
| `scripts/console.py` | Paced, full-duplex access to the device console from a host |
| `scripts/pair.py` | Commissioning helper that keeps the setup passcode and the Thread dataset out of the terminal |
| `scripts/longpress_probe.py` | Gesture capture and analysis for the [long-press question](docs/longpress-experiment.md) |
| `sdkconfig.defaults` | Controller/commissioner, PAA trust store, platform DNS-SD |
| `sdkconfig.defaults.esp32c6` | Native 802.15.4 radio, BLE, OpenThread CLI |
| `partitions.csv` | Single 3.75 MB factory app plus a SPIFFS partition for PAA roots |

`switch_watch.cpp` subscribes to the Switch cluster (`0x003B`) on every
endpoint, acts on `InitialPress`, debounces, and ignores the history a fresh
subscription replays before it is established. Point `kDeviceNodeId` at your
node and replace `on_press()` with whatever you want to drive. It logs one line
per event including the device's own timestamp. Use device timestamps to measure gesture duration without including
delays in event delivery.

## Related

- [ikea-bilresa-e2490](https://github.com/tdamsma/ikea-bilresa-e2490) documents the remote’s Matter endpoints, events, sleep behaviour, and Zigbee mode.
- [esp32c6-zigbee-probe](https://github.com/tdamsma/esp32c6-zigbee-probe) provides the Zigbee coordinator firmware used to capture the remote’s commands.

## Licence

Apache-2.0. Contains code derived from Espressif's esp-matter controller example.
