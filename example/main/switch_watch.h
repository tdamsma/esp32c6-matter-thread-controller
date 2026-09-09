/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

/* Subscribe to a commissioned device's Switch cluster events and act on them.
 * Starts a retrying subscription shortly after boot. */
void switch_watch_start();
