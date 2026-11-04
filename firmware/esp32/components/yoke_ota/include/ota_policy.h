#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Pure admission, journal and boot-health policy shared by target and host
 * tests. No ESP-IDF dependency. */
bool yoke_ota_parse_digest(const char *hex, uint8_t out[32]);
bool yoke_ota_length_allowed(size_t length, size_t slot_size);
bool yoke_ota_parse_length(const char *text, size_t limit, size_t *out);
const char *yoke_ota_boot_state(bool pending_verify, bool journal_pending,
                              bool running_is_target);

/* Status journal: evidence for operators, never trust. It is stored as an
 * explicit little-endian byte layout under the NVS key "journal2" with a
 * version and a compatibility floor; the unversioned layout written by
 * firmware 0.1.0 under "journal" is read once for migration and never
 * rewritten, so a rollback to 0.1.0 still finds the record it expects. */
#define YOKE_JOURNAL_VERSION 1u
#define YOKE_JOURNAL_V1_SIZE 222u
#define YOKE_JOURNAL_LEGACY_SIZE 216u
#define YOKE_JOURNAL_BLOB_MAX 512u

typedef struct {
    uint32_t target_address;
    uint32_t pending;
    char digest[65];
    char version[33];
    char state[24];
    char error[80];
} yoke_journal_t;

typedef enum {
    YOKE_JOURNAL_OK,
    YOKE_JOURNAL_UNKNOWN_VERSION, /* newer writer declared it unreadable here */
    YOKE_JOURNAL_CORRUPT,
} yoke_journal_result_t;

size_t yoke_ota_journal_encode(const yoke_journal_t *journal, uint8_t *blob, size_t cap);
yoke_journal_result_t yoke_ota_journal_decode(const uint8_t *blob, size_t size,
                                              yoke_journal_t *journal);
yoke_journal_result_t yoke_ota_journal_decode_legacy(const uint8_t *blob, size_t size,
                                                     yoke_journal_t *journal);

/* Boot health. Confirm only when everything local works AND either the
 * network is proven (IP acquired) or the link was observed down (a
 * cable-less bench boot must not roll back a locally healthy image). */
typedef struct {
    bool local_ok;        /* trust, storage, W5500 driver, HTTPS listener */
    bool ir_ok;           /* IR outputs created and self-test passed */
    bool listeners_up;    /* IR listener up, or IR API disabled by config */
    bool ip_acquired;
    bool link_up;
    bool link_down_seen;  /* explicit link-down event */
    uint32_t ms_since_eth_start;
    uint32_t link_settle_ms; /* no link-up within this window: observed down */
} yoke_health_inputs_t;

typedef enum { YOKE_HEALTH_WAIT, YOKE_HEALTH_CONFIRM, YOKE_HEALTH_FAIL } yoke_health_t;

yoke_health_t yoke_ota_health_decide(const yoke_health_inputs_t *in);
