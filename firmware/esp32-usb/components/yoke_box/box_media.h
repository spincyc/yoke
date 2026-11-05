#pragma once
/* The USB/IP media description of PROTOCOL.md (reference usbip driver):
 * {"busids": ["<busid>", ...], "host": "<host>", "port": <int>}. Pure C. */
#include <stddef.h>
#include <stdint.h>

#define BOX_USBIP_PORT 3240
#define BOX_BUSID "1-1"   /* the one device on the root port */

typedef struct {
    uint32_t ipv4;        /* host byte order */
    uint16_t port;
    char busid[32];
} box_target_t;

/* Canonical producer media for one exported device; its length, or 0 when
 * it does not fit. */
size_t box_media_producer(char *out, size_t cap, uint32_t ipv4);
/* A producer's media as the consumer's driver reads it: NULL, or why it is
 * refused. The box connects to IPv4 literals only (admission advertises
 * one; there is no resolver) and imports the first busid. */
const char *box_media_parse(const char *media, size_t len, box_target_t *out);
