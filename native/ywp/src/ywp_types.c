/* Names and small predicates of the YWP/1 vocabulary (yoke.core.types). */
#include <string.h>
#include "ywp_types.h"

const char *ywp_role_name(ywp_role_t role)
{
    return role == YWP_CONSUMER ? "consumer" : "producer";
}

const char *ywp_kind_name(ywp_kind_t kind)
{
    return kind == YWP_USB ? "usb" : "hdmi";
}

const char *ywp_state_name(ywp_state_t state)
{
    static const char *const names[] = {"idle", "pending", "active", "error"};
    return (unsigned)state < 4u ? names[state] : "idle";
}

const char *ywp_frame_type_name(ywp_frame_type_t type)
{
    static const char *const names[] = {"ann", "int", "ack", "evt", "bye"};
    return (unsigned)type < 5u ? names[type] : "ann";
}

const char *ywp_drop_name(ywp_drop_t drop)
{
    static const char *const names[] = {"ok", "malformed", "version", "auth",
                                        "stale", "dup", "oversize"};
    return (unsigned)drop < (unsigned)YWP_DROP_COUNT ? names[drop] : "ok";
}

bool ywp_id_valid(const char *id, size_t len)
{
    if (!id || len < 1 || len > YWP_ID_MAX) return false;
    for (size_t i = 0; i < len; ++i) {
        char c = id[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || (i > 0 && c == '-');
        if (!ok) return false;
    }
    return true;
}

int ywp_version_cmp(const ywp_version_t *a, const ywp_version_t *b)
{
    if (a->n != b->n) return a->n < b->n ? -1 : 1;
    int c = strcmp(a->origin, b->origin);
    return (c > 0) - (c < 0);
}
