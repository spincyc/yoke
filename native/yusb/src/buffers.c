/* Backend data buffer layout (see buffers.h). */
#include <stdlib.h>
#include <string.h>
#include "buffers.h"

void ybuf_layout(ybuf_layout_t *l, const yusb_buffers_t *hook)
{
    memset(l, 0, sizeof(*l));
    l->hook.align = 1;
    if (hook && hook->align > 1 && !(hook->align & (hook->align - 1))) {
        l->hook = *hook;
        l->custom = true;
    }
}

size_t ybuf_round(const ybuf_layout_t *l, size_t n)
{
    size_t a = l->hook.align;
    return (n + a - 1) / a * a;
}

size_t ybuf_offset(const ybuf_layout_t *l) { return ybuf_round(l, YUSB_HEADROOM); }

void *ybuf_alloc(const ybuf_layout_t *l, size_t size)
{
    if (!l->custom) return calloc(1, size ? size : 1);
    size = ybuf_round(l, size ? size : 1);
    void *block = l->hook.alloc ? l->hook.alloc(l->hook.ctx, size, l->hook.align)
                                : aligned_alloc(l->hook.align, size);
    if (block && !l->hook.alloc) memset(block, 0, size);
    return block;
}

void ybuf_free(const ybuf_layout_t *l, void *block)
{
    if (!block) return;
    if (l->custom && l->hook.free) l->hook.free(l->hook.ctx, block);
    else free(block);
}
