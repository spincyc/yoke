#include "box_limit.h"

static void refill(box_limit_t *limit, unsigned i, double now)
{
    double tokens = limit->entry[i].tokens + (now - limit->entry[i].at) * BOX_LIMIT_RATE;
    limit->entry[i].tokens = tokens < BOX_LIMIT_BURST ? tokens : BOX_LIMIT_BURST;
    limit->entry[i].at = now;
}

static int find(const box_limit_t *limit, uint32_t addr)
{
    for (unsigned i = 0; i < BOX_LIMIT_SOURCES; ++i)
        if (limit->entry[i].used && limit->entry[i].addr == addr) return (int)i;
    return -1;
}

bool box_limit_allow(box_limit_t *limit, uint32_t addr, double now)
{
    int i = find(limit, addr);
    if (i < 0) return true;
    refill(limit, (unsigned)i, now);
    if (limit->entry[i].tokens >= 1.0) return true;
    limit->throttled++;
    return false;
}

void box_limit_failed(box_limit_t *limit, uint32_t addr, double now)
{
    int i = find(limit, addr);
    if (i < 0) {
        /* A free entry, else the one closest to forgiven. */
        unsigned pick = 0;
        for (unsigned k = 0; k < BOX_LIMIT_SOURCES; ++k) {
            if (!limit->entry[k].used) {
                pick = k;
                break;
            }
            refill(limit, k, now);
            if (limit->entry[k].tokens > limit->entry[pick].tokens) pick = k;
        }
        limit->entry[pick].addr = addr;
        limit->entry[pick].tokens = BOX_LIMIT_BURST;
        limit->entry[pick].at = now;
        limit->entry[pick].used = true;
        i = (int)pick;
    }
    refill(limit, (unsigned)i, now);
    double left = limit->entry[i].tokens - 1.0;
    limit->entry[i].tokens = left > 0.0 ? left : 0.0;
}
