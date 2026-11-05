/* Differential probe: the C core's pure functions over a line protocol, so
 * tests/native_usb/test_native_diff.py can compare them with the Python
 * reference (usbip.py, descriptors.py, policy.py). One request per line,
 * hex for bytes; every answer is one line ending in "\n" (the builder is
 * interactive: "req <setup>" lines answered by "ans <hex>" or "stall").
 *
 *   hdr <hex48>            -> "ok cmd seq devid dir ep flags len sf np iv setup st act ec victim" | "err"
 *   devinfo <hex312>       -> "ok path busid busnum devnum speed vid pid bcd c s p cfg ncfg nif" | "err"
 *   build <speed> <power> <ep0> <in> <out> <num> <prefetch>
 *                          -> req ... / "done" | "fail <code> <why>", then the presentation
 *   decide <cfg> <ifmask> <alts64hex> <halted> <cache> <setup16hex>
 *                          -> "<kind> <hex|-> <reason|-> <value> <iface> <alt> <addr> <halt> <cache>"
 *                             (keeps the remembered descriptor misses)
 *   remember <setup16hex> <stalled> <hex|=>  -> "ok" (yusb_control_remember)
 *   forget                 -> "ok" (a fresh state: no misses, nothing halted)
 *   plan <packet> <limit> <actual:requested>...  -> "count total zero"
 *   cbw <hex>              -> "ok length datain" | "none"
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "yusb_policy.h"
#include "yusb_usbip.h"

static yusb_presentation_t pres;
static yusb_builder_t builder;
static yusb_control_state_t state;

static size_t unhex(const char *text, uint8_t *out, size_t cap)
{
    size_t n = 0;
    while (text[0] && text[1] && n < cap) {
        unsigned v;
        if (sscanf(text, "%2x", &v) != 1) break;
        out[n++] = (uint8_t)v;
        text += 2;
    }
    return n;
}

static void hex(const uint8_t *data, size_t len)
{
    if (!data) { fputs("-", stdout); return; }
    if (!len) fputs("=", stdout);  /* present but empty */
    for (size_t i = 0; i < len; ++i) printf("%02x", data[i]);
}

static void blob(const char *name, yusb_blob_t b)
{
    printf("%s ", name);
    hex(yusb_blob(&pres, b), b.len);
    putchar('\n');
}

static void show_presentation(void)
{
    printf("pres speed %u %u\n", pres.remote_speed, pres.speed);
    blob("device", pres.device_raw);
    for (unsigned i = 0; i < pres.num_configs; ++i) blob("config", pres.config_raw[i]);
    blob("qualifier", pres.qualifier);
    blob("other", pres.other_speed);
    blob("bos", pres.bos);
    for (unsigned i = 0; i < pres.num_strings; ++i) {
        printf("string %u %u ", pres.strings[i].index, pres.strings[i].lang);
        hex(yusb_blob(&pres, pres.strings[i].data), pres.strings[i].data.len);
        putchar('\n');
    }
    for (unsigned i = 0; i < pres.num_class; ++i) {
        printf("class %u %u %u ", pres.class_descs[i].interface, pres.class_descs[i].type,
               pres.class_descs[i].index);
        hex(yusb_blob(&pres, pres.class_descs[i].data), pres.class_descs[i].data.len);
        putchar('\n');
    }
    puts("end");
}

static void cmd_build(char *args)
{
    unsigned speed, power, ep0, in, out, num, prefetch;
    if (sscanf(args, "%u %u %u %u %u %u %u", &speed, &power, &ep0, &in, &out, &num, &prefetch) != 7) {
        puts("fail -2 usage");
        return;
    }
    yusb_budget_t budget = {(uint8_t)in, (uint8_t)out, (uint8_t)num};
    yusb_builder_init(&builder, &pres, speed, power, ep0, &budget);
    builder.prefetch_os = prefetch;
    yusb_setup_t req;
    int rc;
    static char line[300000];
    static uint8_t data[70000];
    while ((rc = yusb_builder_next(&builder, &req)) == 1) {
        uint8_t raw[8];
        yusb_setup_pack(&req, raw);
        printf("req ");
        hex(raw, 8);
        putchar('\n');
        fflush(stdout);
        if (!fgets(line, sizeof(line), stdin)) return;
        if (!strncmp(line, "stall", 5)) yusb_builder_feed(&builder, NULL, 0, true);
        else yusb_builder_feed(&builder, data, unhex(line + 4, data, sizeof(data)), false);
    }
    if (rc < 0) {
        printf("fail %d %s\n", rc, builder.why);
        return;
    }
    puts("done");
    show_presentation();
}

static void cmd_decide(char *args)
{
    unsigned cfg, mask, halted, cache;
    char alts[200], setuphex[40];
    if (sscanf(args, "%u %u %199s %u %u %39s", &cfg, &mask, alts, &halted, &cache, setuphex) != 6) {
        puts("usage");
        return;
    }
    state.configuration = (uint8_t)cfg;
    state.interfaces = mask;
    memset(state.alternates, 0, sizeof(state.alternates));
    unhex(alts, state.alternates, sizeof(state.alternates));
    state.halted = halted;
    uint8_t raw[8] = {0};
    unhex(setuphex, raw, 8);
    yusb_setup_t s = yusb_setup_unpack(raw);
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    policy.descriptor_cache = cache;
    yusb_decision_t d;
    yusb_decide(&s, &pres, &state, &policy, &d);
    static const char *const kinds[] = {"reply", "ack", "stall", "forward", "setconf", "setif", "halt"};
    printf("%s ", kinds[d.kind]);
    if (d.kind == YUSB_D_REPLY) hex(d.data, d.len);
    else fputs("-", stdout);
    printf(" %s %u %u %u %u %u %u\n", d.reason ? "r" : "-", d.value, d.interface, d.alternate,
           d.address, d.halt, d.cache);
    if (d.reason) printf("reason %s\n", d.reason);
    else puts("reason -");
}

int main(void)
{
    static char line[300000];
    setvbuf(stdout, NULL, _IOLBF, 0);
    memset(&pres, 0, sizeof(pres));
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\n")] = 0;
        char *args = strchr(line, ' ');
        if (args) *args++ = 0;
        else args = line + strlen(line);
        if (!strcmp(line, "hdr")) {
            uint8_t raw[48] = {0};
            unhex(args, raw, 48);
            yusb_usbip_hdr_t h;
            if (yusb_usbip_decode(raw, &h)) { puts("err"); continue; }
            printf("ok %u %u %u %u %u %u %u %d %u %d ", h.command, h.seqnum, h.devid, h.direction,
                   h.ep, h.flags, h.length, h.start_frame, h.number_of_packets, h.interval);
            hex(h.setup, 8);
            printf(" %d %u %d %u ", h.status, h.actual_length, h.error_count, h.unlink_seqnum);
            uint8_t again[48];
            yusb_usbip_encode(&h, again);
            hex(again, 48);
            putchar('\n');
        } else if (!strcmp(line, "devinfo")) {
            uint8_t raw[312] = {0};
            unhex(args, raw, 312);
            yusb_device_info_t i;
            if (yusb_device_info_decode(raw, &i)) { puts("err"); continue; }
            printf("ok ");
            hex((const uint8_t *)i.path, strlen(i.path));
            putchar(' ');
            hex((const uint8_t *)i.busid, strlen(i.busid));
            printf(" %u %u %u %u %u %u %u %u %u %u %u %u\n", i.busnum, i.devnum, i.speed, i.vendor, i.product,
                   i.bcd_device, i.device_class, i.device_subclass, i.device_protocol,
                   i.configuration_value, i.num_configurations, i.num_interfaces);
        } else if (!strcmp(line, "build")) {
            cmd_build(args);
        } else if (!strcmp(line, "decide")) {
            cmd_decide(args);
        } else if (!strcmp(line, "remember")) {
            char setuphex[40], datahex[1024];
            unsigned stalled;
            if (sscanf(args, "%39s %u %1023s", setuphex, &stalled, datahex) != 3) { puts("usage"); continue; }
            uint8_t raw[8] = {0}, data[512];
            unhex(setuphex, raw, 8);
            yusb_setup_t s = yusb_setup_unpack(raw);
            size_t n = datahex[0] == '=' ? 0 : unhex(datahex, data, sizeof(data));
            yusb_control_remember(&state, &s, data, n, stalled);
            puts("ok");
        } else if (!strcmp(line, "forget")) {
            memset(&state, 0, sizeof(state));
            puts("ok");
        } else if (!strcmp(line, "plan")) {
            unsigned packet, limit;
            int used;
            if (sscanf(args, "%u %u%n", &packet, &limit, &used) != 2) { puts("usage"); continue; }
            uint32_t actual[64], requested[64];
            size_t n = 0;
            char *p = args + used;
            unsigned a, r;
            int k;
            while (n < 64 && sscanf(p, " %u:%u%n", &a, &r, &k) == 2) {
                actual[n] = a;
                requested[n++] = r;
                p += k;
            }
            uint32_t total;
            bool zero;
            size_t count = yusb_in_write_plan(actual, requested, n, packet, limit, &total, &zero);
            printf("%zu %u %d\n", count, total, zero);
        } else if (!strcmp(line, "cbw")) {
            uint8_t raw[64];
            size_t n = unhex(args, raw, sizeof(raw));
            uint32_t length;
            bool in;
            if (yusb_parse_cbw(raw, n, &length, &in)) printf("ok %u %d\n", length, in);
            else puts("none");
        } else {
            puts("unknown");
        }
        fflush(stdout);
    }
    return 0;
}
