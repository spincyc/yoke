#include "box_media.h"
#include <stdio.h>
#include <string.h>
#include "ywp_json.h"
#include "yusb_fence.h"

size_t box_media_producer(char *out, size_t cap, uint32_t ipv4)
{
    char host[16];
    snprintf(host, sizeof(host), "%u.%u.%u.%u", (unsigned)(ipv4 >> 24), (unsigned)(ipv4 >> 16) & 255u,
             (unsigned)(ipv4 >> 8) & 255u, (unsigned)ipv4 & 255u);
    ywp_out_t o;
    if (!cap) return 0;
    ywp_out_buffer(&o, (uint8_t *)out, cap - 1); /* room for the NUL */
    ywp_out_text(&o, "{\"busids\":[");
    ywp_out_str(&o, BOX_BUSID, strlen(BOX_BUSID));
    ywp_out_text(&o, "],\"host\":");
    ywp_out_str(&o, host, strlen(host));
    ywp_out_text(&o, ",\"port\":");
    ywp_out_uint(&o, BOX_USBIP_PORT);
    ywp_out_text(&o, "}");
    if (!ywp_out_finish(&o)) return 0;
    out[o.len] = 0;
    return o.len;
}

const char *box_media_parse(const char *media, size_t len, box_target_t *out)
{
    static ywp_json_t doc; /* about 5 KiB: never on a task stack */
    if (len > YWP_MAX_FRAME || !ywp_json_parse(&doc, (const uint8_t *)media, len) ||
        ywp_json_type(&doc, 0) != YWP_JSON_OBJECT)
        return "media-not-a-json-object";
    unsigned host = ywp_json_get(&doc, 0, "host"), port = ywp_json_get(&doc, 0, "port");
    unsigned busids = ywp_json_get(&doc, 0, "busids");
    char text[YUSB_HOST_MAX + 1], ipv4[16];
    if (host == YWP_JSON_NONE || ywp_json_type(&doc, host) != YWP_JSON_STRING ||
        ywp_json_str_copy(&doc, host, text, sizeof(text)) == SIZE_MAX)
        return "media-host-missing";
    if (yusb_validate_ipv4(text, strlen(text), ipv4)) return "media-host-must-be-ipv4";
    uint64_t number;
    if (port == YWP_JSON_NONE || !ywp_json_uint(&doc, port, 65535, &number) || !number)
        return "media-port-invalid";
    unsigned first = busids == YWP_JSON_NONE || ywp_json_type(&doc, busids) != YWP_JSON_ARRAY
                         ? YWP_JSON_NONE : ywp_json_at(&doc, busids, 0);
    if (first == YWP_JSON_NONE || ywp_json_type(&doc, first) != YWP_JSON_STRING ||
        ywp_json_str_copy(&doc, first, out->busid, sizeof(out->busid)) == SIZE_MAX ||
        yusb_validate_busid(out->busid, strlen(out->busid)))
        return "media-busids-invalid";
    unsigned a, b, c, d;
    sscanf(ipv4, "%u.%u.%u.%u", &a, &b, &c, &d);
    out->ipv4 = a << 24 | b << 16 | c << 8 | d;
    out->port = (uint16_t)number;
    return NULL;
}
