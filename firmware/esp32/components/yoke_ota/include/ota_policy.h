#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Pure admission and boot classification shared by target and host tests. */
bool yoke_ota_parse_digest(const char *hex, uint8_t out[32]);
bool yoke_ota_length_allowed(size_t length, size_t slot_size);
bool yoke_ota_parse_length(const char *text, size_t limit, size_t *out);
const char *yoke_ota_boot_state(bool pending_verify, bool journal_pending,
                              bool running_is_target);
