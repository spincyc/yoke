/* YX codec driver for the independent Python oracle (test_native_yx.py).
 *
 *   yx_tool enc <levers> <cache_blocks>
 *       stdin:  { u32 BE length, length bytes }*
 *       stdout: { u32 BE wire length, wire bytes }* from one encoder
 *   yx_tool dec <levers> <cache_blocks>
 *       stdin:  { u32 BE length, u32 BE wire length, wire bytes }*
 *       stdout: { i32 BE status, u32 BE consumed, length bytes if status 0 }*
 *               from one decoder, stopping after the first failure
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "yusb_yx.h"

static uint8_t *input;
static size_t input_len, input_at;

static void die(const char *why)
{
    fprintf(stderr, "yx_tool: %s\n", why);
    exit(2);
}

static void read_all(void)
{
    size_t cap = 1u << 16;
    input = malloc(cap);
    if (!input) die("out of memory");
    for (size_t n; (n = fread(input + input_len, 1, cap - input_len, stdin)) > 0;) {
        input_len += n;
        if (input_len == cap && !(input = realloc(input, cap *= 2u))) die("out of memory");
    }
}

static const uint8_t *take(size_t n)
{
    if (input_len - input_at < n) die("truncated input");
    input_at += n;
    return input + input_at - n;
}

static uint32_t take_u32(void)
{
    const uint8_t *p = take(4);
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static void put_u32(uint32_t v)
{
    uint8_t p[4] = {(uint8_t)(v >> 24), (uint8_t)(v >> 16), (uint8_t)(v >> 8), (uint8_t)v};
    fwrite(p, 1, 4, stdout);
}

int main(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[1], "enc") && strcmp(argv[1], "dec")))
        die("usage: yx_tool enc|dec <levers> <cache_blocks>");
    yusb_yx_params_t agreed = {YUSB_YX_VERSION, (uint8_t)strtoul(argv[2], NULL, 0),
                               (uint16_t)strtoul(argv[3], NULL, 0)};
    bool encode = !strcmp(argv[1], "enc");
    yusb_yx_enc_t enc;
    yusb_yx_dec_t *dec = calloc(1, sizeof(*dec));
    if (!dec) die("out of memory");
    if (encode ? yusb_yx_enc_init(&enc, &agreed) : yusb_yx_dec_init(dec, &agreed)) die("init");
    read_all();
    while (input_at < input_len) {
        size_t length = take_u32();
        if (encode) {
            const uint8_t *src = take(length);
            uint8_t *wire = malloc(yusb_yx_bound(length) + 1u);
            if (!wire) die("out of memory");
            size_t n = yusb_yx_encode(&enc, src, length, wire);
            put_u32((uint32_t)n);
            fwrite(wire, 1, n, stdout);
            free(wire);
            continue;
        }
        size_t wire_len = take_u32(), used = 0;
        const uint8_t *wire = take(wire_len);
        uint8_t *dst = malloc(length + 1u);
        if (!dst) die("out of memory");
        int err = yusb_yx_decode(dec, wire, wire_len, dst, length, &used);
        put_u32((uint32_t)err);
        put_u32((uint32_t)used);
        if (!err) fwrite(dst, 1, length, stdout);
        free(dst);
        if (err) break;
    }
    if (encode) yusb_yx_enc_free(&enc);
    yusb_yx_dec_free(dec);
    free(dec);
    free(input);
    return fflush(stdout) ? 2 : 0;
}
