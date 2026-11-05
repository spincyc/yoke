/* Line-protocol driver for the native frame layer (host tests only).
 *
 *   key <hex|->                     set the site key ("-": open mode)
 *   dec <hex>                       -> drop <reason> | ok <type> <origin> <seq>
 *                                      <ts bits> <canonical body hex> <fingerprint hex>
 *   enc <type> <origin> <seq> <ts bits> <body hex>  -> ok <hex> | err
 *   can <hex>                       -> ok <canonical hex> | invalid
 *   ovh <origin>                    -> ok <bytes>
 *
 * Binary fields are lowercase hex; doubles travel as their 64-bit pattern so
 * nothing is lost to formatting. Built with ASan and UBSan. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ywp_frame.h"
#include "ywp_json.h"

#define LINE_MAX_BYTES (64 * 1024)

static char line[LINE_MAX_BYTES];
static uint8_t bin[LINE_MAX_BYTES / 2];
static uint8_t outbuf[8 * YWP_MAX_FRAME];
static ywp_codec_t codec;
static ywp_json_t doc;

static size_t unhex(const char *hex, uint8_t *out)
{
    size_t n = 0;
    if (strcmp(hex, "-") == 0) return 0;
    for (; hex[0] && hex[1]; hex += 2) {
        unsigned v;
        if (sscanf(hex, "%2x", &v) != 1) break;
        out[n++] = (uint8_t)v;
    }
    return n;
}

static void put_hex(const uint8_t *p, size_t n)
{
    if (!n) fputc('-', stdout);
    for (size_t i = 0; i < n; ++i) printf("%02x", p[i]);
}

static double from_bits(const char *hex)
{
    unsigned long long bits = strtoull(hex, NULL, 16);
    double v;
    memcpy(&v, &bits, sizeof(v));
    return v;
}

static void cmd_dec(char *hex)
{
    size_t len = unhex(hex, bin);
    ywp_frame_t f;
    ywp_drop_t verdict = ywp_decode(&codec, bin, len, &f);
    if (verdict != YWP_OK) {
        printf("drop %s\n", ywp_drop_name(verdict));
        return;
    }
    ywp_frame_fingerprint(&codec, &f);
    unsigned long long bits;
    memcpy(&bits, &f.ts, sizeof(bits));
    ywp_out_t o;
    ywp_out_buffer(&o, outbuf, sizeof(outbuf));
    ywp_json_canonical(&codec.json, f.body, YWP_JSON_NONE, &o);
    printf("ok %s %s %llu %016llx ", ywp_frame_type_name(f.type), f.origin,
           (unsigned long long)f.seq, bits);
    put_hex(outbuf, o.len);
    fputc(' ', stdout);
    put_hex(f.fingerprint, sizeof(f.fingerprint));
    fputc('\n', stdout);
}

static void cmd_enc(char *args)
{
    char type[8], origin[64], seq[32], ts[32];
    static char body[LINE_MAX_BYTES];
    if (sscanf(args, "%7s %63s %31s %31s %65535s", type, origin, seq, ts, body) != 5) {
        puts("err");
        return;
    }
    int t = 0;
    while (t < 5 && strcmp(ywp_frame_type_name((ywp_frame_type_t)t), type) != 0) ++t;
    size_t len = unhex(body, bin);
    uint8_t frame[YWP_MAX_FRAME];
    size_t n = ywp_encode(&codec, (ywp_frame_type_t)t, origin, strtoull(seq, NULL, 10),
                          from_bits(ts), bin, len, frame);
    if (t == 5 || !n) {
        puts("err");
        return;
    }
    fputs("ok ", stdout);
    put_hex(frame, n);
    fputc('\n', stdout);
}

static void cmd_can(char *hex)
{
    size_t len = unhex(hex, bin);
    if (!ywp_json_parse(&doc, bin, len)) {
        puts("invalid");
        return;
    }
    ywp_out_t o;
    ywp_out_buffer(&o, outbuf, sizeof(outbuf));
    ywp_json_canonical(&doc, 0, YWP_JSON_NONE, &o);
    fputs("ok ", stdout);
    put_hex(outbuf, o.len);
    fputc('\n', stdout);
}

int main(void)
{
    ywp_codec_init(&codec, &ywp_crypto_portable, NULL, 0);
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\r\n")] = 0;
        char *arg = strchr(line, ' ');
        if (arg) *arg++ = 0;
        else arg = line + strlen(line);
        if (strcmp(line, "key") == 0) {
            static uint8_t key[4096];
            size_t n = unhex(arg, key);
            ywp_codec_init(&codec, &ywp_crypto_portable, key, n);
            puts("ok");
        } else if (strcmp(line, "dec") == 0) {
            cmd_dec(arg);
        } else if (strcmp(line, "enc") == 0) {
            cmd_enc(arg);
        } else if (strcmp(line, "can") == 0) {
            cmd_can(arg);
        } else if (strcmp(line, "ovh") == 0) {
            printf("ok %zu\n", ywp_frame_overhead(arg));
        } else {
            puts("err");
        }
    }
    return 0;
}
