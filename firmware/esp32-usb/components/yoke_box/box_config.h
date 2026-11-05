#pragma once
/* USB box configuration (NVS namespace yoke_usb, written over the mutual-TLS
 * admin API): the USB role, and the YWP/1 identity and site key. The box
 * bridges USB/IP and pairs over YWP/1 once it has a node id and either a
 * PSK of at least 32 bytes or an explicit open mode; until then it runs its
 * hardware self-test. Pure C. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BOX_ID_MAX 32          /* YWP_ID_MAX */
#define BOX_NAME_MAX 256       /* UTF-8 bytes (YWP allows 64 code points) */
#define BOX_PSK_MIN 32         /* MIN_PSK_BYTES of the reference agent */
#define BOX_PSK_MAX 256

typedef enum { BOX_ROLE_DEVICE, BOX_ROLE_COMPUTER } box_role_t;

typedef struct {
    box_role_t role;
    char node_id[BOX_ID_MAX + 1];   /* "": YWP not configured */
    char name[BOX_NAME_MAX + 1];
    uint8_t psk[BOX_PSK_MAX];
    size_t psk_len;                 /* 0, or BOX_PSK_MIN..BOX_PSK_MAX */
    bool open;                      /* explicit open mode: no site key */
} box_config_t;

const char *box_role_name(box_role_t role);
bool box_role_parse(const char *text, box_role_t *out);
/* One admin field; NULL or the error reported to the client. Text fields:
 * role, node_id, name, psk ("" clears it). Flag field: open. The PSK is
 * used byte for byte, as the reference agent uses its PSK file (for the
 * recommended file of 64 hex characters: those 64 characters). */
const char *box_config_set_text(box_config_t *config, const char *key, const char *value);
const char *box_config_set_flag(box_config_t *config, const char *key, bool value);
/* Whole-configuration rules, after every field; NULL when consistent. */
const char *box_config_check(const box_config_t *config);
/* Ready to pair and bridge (otherwise: hardware self-test). */
bool box_config_bridging(const box_config_t *config);
