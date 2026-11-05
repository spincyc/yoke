#include "box_time.h"

#define KEPT_MAGIC 0x594b434cu /* "YKCL" */

box_time_step_t box_time_step(int64_t now_s, int64_t unix_s, bool force)
{
    if (unix_s < now_s) return BOX_TIME_BACKWARDS;
    if (unix_s - now_s > BOX_TIME_STEP_MAX && !force) return BOX_TIME_TOO_FAR;
    return BOX_TIME_OK;
}

static uint32_t check_of(int64_t unix_s)
{
    uint64_t v = (uint64_t)unix_s;
    return (uint32_t)v ^ (uint32_t)(v >> 32) ^ ~KEPT_MAGIC;
}

void box_time_keep(box_time_kept_t *kept, int64_t unix_s)
{
    kept->unix_s = unix_s;
    kept->check = check_of(unix_s);
    kept->magic = KEPT_MAGIC;
}

int64_t box_time_restore(box_time_kept_t *kept, bool reset_kept_rtc, int64_t floor_s)
{
    bool valid = reset_kept_rtc && kept->magic == KEPT_MAGIC &&
                 kept->check == check_of(kept->unix_s) &&
                 kept->unix_s >= floor_s - BOX_TIME_KEPT_BEHIND &&
                 kept->unix_s <= floor_s + BOX_TIME_KEPT_AHEAD;
    int64_t unix_s = kept->unix_s;
    kept->magic = 0;
    if (!valid) return -1;
    return unix_s > floor_s ? unix_s : floor_s;
}
