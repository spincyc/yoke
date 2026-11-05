/* Line driver for test_ywp_num.py: one output line per input line.
 *   "p <literal>"         -> "inf" when ywp_num_parse fails, else the
 *                            16-hex-digit bit pattern of the double
 *   "f <literal>"         -> "1" or "0", ywp_num_finite
 *   "c <literal>"         -> ywp_num_canonical ("" for a non-number)
 *   "r <16 hex digits>"   -> ywp_num_repr of that bit pattern
 * Each literal is copied into an exact-size heap buffer with no terminator,
 * so AddressSanitizer catches any read past `len`. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ywp_num.h"

static int fail(const char *why, const char *line)
{
    fprintf(stderr, "num_check: %s: %.80s\n", why, line);
    return 2;
}

static bool put_text(const char *out, size_t n)
{
    if (n >= YWP_REPR_MAX || out[n] || strlen(out) != n) return false;
    puts(out);
    return true;
}

int main(void)
{
    static char line[1 << 16];
    while (fgets(line, sizeof(line), stdin)) {
        size_t len = strlen(line);
        if (len && line[len - 1] == '\n') line[--len] = 0;
        else if (!feof(stdin)) return fail("line too long", line);
        if (len < 2 || line[1] != ' ') return fail("bad line", line);
        char cmd = line[0], out[YWP_REPR_MAX];
        memset(out, 'x', sizeof(out));
        if (cmd == 'p' || cmd == 'f' || cmd == 'c') {
            size_t n = len - 2;
            char *lit = malloc(n ? n : 1u);
            if (!lit) return fail("out of memory", line);
            memcpy(lit, line + 2, n);
            double v;
            bool ok = cmd == 'p' ? ywp_num_parse(lit, n, &v)
                    : cmd == 'f' ? ywp_num_finite(lit, n)
                                 : put_text(out, ywp_num_canonical(lit, n, out));
            free(lit);
            if (cmd == 'c') {
                if (!ok) return fail("bad canonical", line);
            } else if (cmd == 'f') {
                puts(ok ? "1" : "0");
            } else if (!ok) {
                puts("inf");
            } else {
                uint64_t bits;
                memcpy(&bits, &v, sizeof(bits));
                printf("%016llx\n", (unsigned long long)bits);
            }
        } else if (cmd == 'r') {
            char *end;
            uint64_t bits = strtoull(line + 2, &end, 16);
            if (*end || end != line + 18) return fail("bad bit pattern", line);
            double v;
            memcpy(&v, &bits, sizeof(v));
            if (!put_text(out, ywp_num_repr(v, out))) return fail("bad repr", line);
        } else {
            return fail("unknown command", line);
        }
    }
    return ferror(stdin) ? fail("read error", "") : 0;
}
