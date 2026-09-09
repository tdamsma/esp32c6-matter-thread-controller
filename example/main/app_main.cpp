/* Minimal Matter controller on an ESP32-C6: forms its own Thread network,
 * registers Thread services itself, commissions a device over BLE, and
 * subscribes to its events. No border router, no hub, no PSRAM.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <esp_err.h>
#include <esp_log.h>
#include <nvs_flash.h>

#include <esp_matter.h>
#include <esp_matter_console.h>
#include <esp_matter_controller_client.h>
#include <esp_matter_controller_console.h>

#include <esp_openthread.h>
#include <esp_openthread_lock.h>
#include <esp_openthread_types.h>
#include <openthread/srp_server.h>
#include <platform/ESP32/OpenthreadLauncher.h>

#include "switch_watch.h"

static const char *TAG = "app_main";

/* Fabric id, node id and listen port for this controller. Any values work as
 * long as they stay stable across reboots; they are persisted in NVS. */
constexpr uint64_t kControllerNodeId = 112233;
constexpr uint16_t kControllerFabricId = 1;
constexpr uint16_t kControllerListenPort = 5580;

static void app_event_cb(const ChipDeviceEvent *event, intptr_t arg) {}

extern "C" void app_main()
{
    nvs_flash_init();

    esp_matter::console::diagnostics_register_commands();
    esp_matter::console::factoryreset_register_commands();
    esp_matter::console::controller_register_commands();
    esp_matter::console::otcli_register_commands();
    esp_matter::console::init();

    /* The upstream controller example only configures OpenThread for border
     * router builds, and panics without this in a native-radio build. */
    esp_openthread_platform_config_t config = {};
    config.radio_config.radio_mode = RADIO_MODE_NATIVE;
    config.host_config.host_connection_mode = HOST_CONNECTION_MODE_NONE;
    config.port_config.storage_partition_name = "nvs";
    config.port_config.netif_queue_size = 10;
    config.port_config.task_queue_size = 10;
    set_openthread_platform_config(&config);

    ESP_ERROR_CHECK(esp_matter::start(app_event_cb));

    /* Be the SRP server too. A commissioned Thread device registers its
     * operational service here, and without it the controller completes
     * commissioning and then cannot resolve the node it just paired. */
    esp_openthread_lock_acquire(portMAX_DELAY);
    otSrpServerSetEnabled(esp_openthread_get_instance(), true);
    esp_openthread_lock_release();
    ESP_LOGI(TAG, "SRP server enabled");

    esp_matter::lock::ScopedChipStackLock lock(portMAX_DELAY);
    auto &controller = esp_matter::controller::matter_controller_client::get_instance();
    controller.init(kControllerNodeId, kControllerFabricId, kControllerListenPort);
    controller.setup_commissioner();

    switch_watch_start();
}
