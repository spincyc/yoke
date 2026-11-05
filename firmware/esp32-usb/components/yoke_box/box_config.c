#include "box_config.h"
#include <string.h>
#include "ywp_json.h"
#include "ywp_types.h"

const char *box_role_name(box_role_t role)
{
    return role == BOX_ROLE_DEVICE ? "device" : "computer";
}

bool box_role_parse(const char *text, box_role_t *out)
{
    if (!strcmp(text, "device")) *out = BOX_ROLE_DEVICE;
    else if (!strcmp(text, "computer")) *out = BOX_ROLE_COMPUTER;
    else return false;
    return true;
}

static const char *set_psk(box_config_t *config, const char *text)
{
    size_t n = strlen(text);
    if (n && (n < BOX_PSK_MIN || n > BOX_PSK_MAX)) return "psk-must-be-32-to-256-bytes";
    memcpy(config->psk, text, n);
    config->psk_len = n;
    return NULL;
}

const char *box_config_set_text(box_config_t *config, const char *key, const char *value)
{
    if (!strcmp(key, "role"))
        return box_role_parse(value, &config->role) ? NULL : "role-must-be-device-or-computer";
    if (!strcmp(key, "node_id")) {
        if (*value && !ywp_id_valid(value, strlen(value))) return "node-id-must-match-[a-z0-9][a-z0-9-]{0,31}";
        strcpy(config->node_id, value);
        return NULL;
    }
    if (!strcmp(key, "name")) {
        /* The node's own rule (ywp_node_create), so a saved name starts. */
        size_t len = strlen(value), chars = 0;
        for (size_t i = 0; i < len; ++i) chars += ((uint8_t)value[i] & 0xc0u) != 0x80u;
        if (len > BOX_NAME_MAX || chars > YWP_NAME_MAX ||
            !ywp_utf8_valid((const uint8_t *)value, len))
            return "name-must-be-utf8-of-at-most-64-characters";
        strcpy(config->name, value);
        return NULL;
    }
    if (!strcmp(key, "psk")) return set_psk(config, value);
    return !strcmp(key, "open") ? "open-must-be-boolean" : "unknown-field";
}

const char *box_config_set_flag(box_config_t *config, const char *key, bool value)
{
    if (strcmp(key, "open")) return "unknown-or-mistyped-field";
    config->open = value;
    return NULL;
}

const char *box_config_check(const box_config_t *config)
{
    if (config->open && config->psk_len) return "open-mode-must-not-have-a-psk";
    return NULL;
}

bool box_config_bridging(const box_config_t *config)
{
    return config->node_id[0] && (config->open || config->psk_len >= BOX_PSK_MIN);
}
