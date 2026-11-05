#pragma once
/* Private: backend data buffers laid out per yusb_buffers_t (yusb.h). A
 * block holds payloads at an aligned offset with YUSB_HEADROOM writable
 * bytes before it; the core's "buf" convention is data - YUSB_HEADROOM. */
#include "yusb.h"

typedef struct {
    yusb_buffers_t hook;      /* align >= 1 */
    bool custom;
} ybuf_layout_t;

void ybuf_layout(ybuf_layout_t *l, const yusb_buffers_t *hook);
/* Payload offset inside a block and a size rounded to the alignment. */
size_t ybuf_offset(const ybuf_layout_t *l);
size_t ybuf_round(const ybuf_layout_t *l, size_t n);
/* Zeroed block of `size` bytes (rounded up); NULL on failure. */
void *ybuf_alloc(const ybuf_layout_t *l, size_t size);
void ybuf_free(const ybuf_layout_t *l, void *block);
/* The "buf" pointer (data - YUSB_HEADROOM) of the payload at `offset`. */
static inline uint8_t *ybuf_at(const ybuf_layout_t *l, void *block, size_t offset)
{
    return (uint8_t *)block + offset + ybuf_offset(l) - YUSB_HEADROOM;
}
