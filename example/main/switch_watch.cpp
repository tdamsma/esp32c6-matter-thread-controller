/* Subscribing to Switch cluster events from a battery-powered Matter device.
 *
 * The one thing that is easy to get wrong is at the bottom of
 * start_subscription(): the event path must be marked URGENT. Without it a
 * sleepy device is free to hold events until its next scheduled report, which
 * it may negotiate to many minutes -- so events are recorded on the device and
 * never delivered, with no error anywhere.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "switch_watch.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <inttypes.h>
#include <stdio.h>

#include <esp_matter_controller_cluster_command.h>
#include <esp_matter_controller_subscribe_command.h>
#include <platform/CHIPDeviceLayer.h>

namespace {
constexpr auto kTag = "switch_watch";

/* Node id you gave the device when pairing it. */
constexpr uint64_t kDeviceNodeId = 1;

constexpr uint32_t kSwitchCluster = 0x003B;
constexpr uint32_t kIdentifyCluster = 0x0003;
constexpr uint32_t kIdentifyCommand = 0;

/* Switch cluster event ids. A single press is InitialPress then ShortRelease;
 * a repeated press adds MultiPressOngoing and MultiPressComplete. Devices that
 * advertise MomentarySwitchLongPress (FeatureMap bit 0x08) also emit LongPress
 * (2) and LongRelease (4). Acting on InitialPress alone gives one action per
 * physical press. */
constexpr uint32_t kInitialPress = 1;

/* Set false to observe a device without touching it: the acknowledgement below
 * wakes it mid-gesture, which contaminates any timing measurement. */
constexpr bool kAcknowledgePress = true;

constexpr uint16_t kMinIntervalSeconds = 0;
constexpr uint16_t kMaxIntervalSeconds = 60;
constexpr int64_t kDebounceUs = 150 * 1000;
constexpr uint32_t kStartDelaySeconds = 20;
constexpr uint32_t kRetryDelaySeconds = 30;

using namespace esp_matter::controller;
using chip::app::AttributePathParams;
using chip::app::EventPathParams;
using chip::Platform::ScopedMemoryBufferWithSize;

bool subscribed = false;
bool seen_any = false;
uint64_t highest_event_number = 0;
int64_t last_action_us[16] = {};

void start_subscription(chip::System::Layer *, void *);

void retry()
{
    subscribed = false;
    chip::DeviceLayer::SystemLayer().StartTimer(chip::System::Clock::Seconds32(kRetryDelaySeconds),
                                                start_subscription, nullptr);
}

/* Render an event's payload as "tag=value" pairs. Switch events carry only small
 * unsigned integers, so this avoids pulling in the generated decoders. */
void format_fields(chip::TLV::TLVReader *data, char *out, size_t size)
{
    out[0] = '\0';
    if (!data) return;
    chip::TLV::TLVReader reader;
    reader.Init(*data);
    chip::TLV::TLVType outer;
    if (reader.EnterContainer(outer) != CHIP_NO_ERROR) return;
    size_t used = 0;
    while (reader.Next() == CHIP_NO_ERROR && used + 1 < size) {
        uint64_t value = 0;
        if (!chip::TLV::IsContextTag(reader.GetTag()) || reader.Get(value) != CHIP_NO_ERROR) continue;
        const int written = snprintf(out + used, size - used, "%s%" PRIu32 "=%" PRIu64, used ? "," : "",
                                     chip::TLV::TagNumFromTag(reader.GetTag()), value);
        if (written < 0 || static_cast<size_t>(written) >= size - used) break;
        used += static_cast<size_t>(written);
    }
}

/* Replace this with whatever the press should do. */
void on_press(uint16_t endpoint)
{
    ESP_LOGI(kTag, "press on endpoint %u", endpoint);

    if (!kAcknowledgePress) return;

    /* Optional acknowledgement: many devices blink an LED on Identify. A sleepy
     * device is in active mode for about a second after a press, so a command
     * sent from here reaches it straight away instead of waiting for its next
     * idle poll. Some devices report IdentifyType None and blink anyway. */
    auto *ack = chip::Platform::New<cluster_command>(kDeviceNodeId, endpoint, kIdentifyCluster,
                                                     kIdentifyCommand, "{\"0:U16\":3}");
    if (ack && ack->send_command() != ESP_OK) { // send_command owns and frees it.
        ESP_LOGW(kTag, "acknowledgement not sent");
    }
}

void on_event(uint64_t node_id, const chip::app::EventHeader &header, chip::TLV::TLVReader *data,
              const chip::app::StatusIB *status)
{
    const uint16_t endpoint = header.mPath.mEndpointId;
    if (node_id != kDeviceNodeId || !data || (status && !status->IsSuccess()) ||
        header.mPath.mClusterId != kSwitchCluster) {
        return;
    }
    /* The timestamp is the device's own, so differences between two events
     * measure the gesture rather than this node's poll cadence. Parsed by
     * scripts/longpress_probe.py. */
    char fields[48];
    format_fields(data, fields, sizeof(fields));
    ESP_LOGI(kTag, "event ep=%u id=%" PRIu32 " num=%" PRIu64 " t=%" PRIu64 "%s fields=[%s]%s", endpoint,
             header.mPath.mEventId, header.mEventNumber, header.mTimestamp.mValue,
             header.mTimestamp.IsEpoch() ? "us_epoch" : "ms_uptime", fields,
             subscribed ? "" : " history");

    /* A new subscription replays the device's stored events before it is
     * established. Use them only to learn where the live stream starts. */
    if (seen_any && header.mEventNumber <= highest_event_number) return;
    seen_any = true;
    highest_event_number = header.mEventNumber;
    if (!subscribed || header.mPath.mEventId != kInitialPress) return;

    const int64_t now = esp_timer_get_time();
    if (endpoint < sizeof(last_action_us) / sizeof(last_action_us[0])) {
        if (now - last_action_us[endpoint] < kDebounceUs) return;
        last_action_us[endpoint] = now;
    }
    on_press(endpoint);
}

class switch_subscription final : public subscribe_command {
public:
    switch_subscription(ScopedMemoryBufferWithSize<AttributePathParams> &&attributes,
                        ScopedMemoryBufferWithSize<EventPathParams> &&events)
        : subscribe_command(
              kDeviceNodeId, std::move(attributes), std::move(events), kMinIntervalSeconds, kMaxIntervalSeconds,
              true /* auto resubscribe */, nullptr, on_event,
              [](uint64_t, uint32_t) { subscribed = true; ESP_LOGI(kTag, "subscribed"); },
              [](uint64_t, uint32_t) { retry(); },
              [](void *, const chip::ScopedNodeId &, CHIP_ERROR) { retry(); }, false)
    {
    }

    CHIP_ERROR OnResubscriptionNeeded(chip::app::ReadClient *client, CHIP_ERROR error) override
    {
        subscribed = false;
        return subscribe_command::OnResubscriptionNeeded(client, error);
    }
};

void start_subscription(chip::System::Layer *, void *)
{
    ScopedMemoryBufferWithSize<AttributePathParams> attributes;
    ScopedMemoryBufferWithSize<EventPathParams> events;
    events.Alloc(1);
    if (!events.Get()) {
        retry();
        return;
    }
    /* Wildcard endpoint and event on the Switch cluster, marked URGENT. The
     * urgent flag is the difference between events arriving in milliseconds and
     * not arriving at all. esp-matter's single-path subscribe_command
     * constructor cannot set it, which is why the paths are built by hand. */
    events[0] = EventPathParams(chip::kInvalidEndpointId, kSwitchCluster, chip::kInvalidEventId,
                                true /* urgent */);

    auto *subscription = chip::Platform::New<switch_subscription>(std::move(attributes), std::move(events));
    if (!subscription) {
        retry();
        return;
    }
    if (subscription->send_command() != ESP_OK) { // send_command owns and frees it.
        retry();
    }
}
} // namespace

void switch_watch_start()
{
    /* After a reboot the device's SRP registration has to be re-learned before
     * it can be resolved, so the first attempt may fail; start_subscription
     * retries on its own. */
    chip::DeviceLayer::SystemLayer().StartTimer(chip::System::Clock::Seconds32(kStartDelaySeconds),
                                                start_subscription, nullptr);
}
