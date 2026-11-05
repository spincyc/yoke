#pragma once
/* ADR 0022 control-socket schema for the native gadget worker (reference:
 * src/yoke/usb/control.py). One JSON object per line over a Unix stream
 * socket; the schema is closed and size-bounded, so neither direction can
 * carry USB payload. The agent's usb-gadget driver supervises the Python
 * and the native worker through these same bytes.
 *
 * Requests:  {"schema":1,"op":"status"}
 *            {"schema":1,"op":"route","generation":N,"target":T|null}
 *            T = {"host":H,"port":P,"busid":B,"source":S|null}
 * Replies:   {"ok":true,"status":{...WorkerStatus.to_json()...}}
 *            {"error":"...","ok":false}
 * Pure: no I/O, no allocation.
 */
#include "yusb_fence.h"

#define YCTL_SCHEMA 1
#define YCTL_MAX_MESSAGE 16384u

typedef struct {
    bool route;              /* false: a status request */
    uint64_t generation;     /* route: 1 .. 2^63 - 1 */
    bool has_target;         /* route: false means "target": null (detach) */
    yusb_target_t target;    /* validated, canonical (control.Target) */
} yctl_request_t;

/* Why a request was refused. Categories, not control.py's exact texts. */
typedef enum {
    YCTL_E_NONE,
    YCTL_E_LINE,        /* not one '\n'-terminated line within YCTL_MAX_MESSAGE */
    YCTL_E_SYNTAX,      /* not JSON of the request grammar (see control.c) */
    YCTL_E_SCHEMA,      /* "schema" missing or not the integer 1 */
    YCTL_E_REQUEST,     /* unknown op, unknown, missing or duplicate keys */
    YCTL_E_GENERATION,  /* not an integer in 1 .. 2^63 - 1 */
    YCTL_E_TARGET,      /* target is not an object of exactly the four keys */
    YCTL_E_HOST,
    YCTL_E_PORT,
    YCTL_E_BUSID,
    YCTL_E_SOURCE,
} yctl_error_t;

/* Parse one request: `line` holds the bytes up to and including the first
 * '\n' (control.read_line). YUSB_OK with *out filled, or YUSB_ERR_PROTOCOL
 * with *out zeroed and *why set (why may be NULL). Anything accepted here
 * control.parse_request(control.decode(line)) accepts with the same route;
 * the converse holds except for the strictness listed in control.c. */
int yctl_parse_request(const char *line, size_t len, yctl_request_t *out, yctl_error_t *why);
/* Short reply text for a category, in control.py's wording where it has one. */
const char *yctl_error_text(yctl_error_t why);

typedef struct {
    const char *key;
    const char *value;
} yctl_pair_t;

typedef struct {
    const char *name;
    uint64_t value;
} yctl_counter_t;

/* control.WorkerStatus. Strings are UTF-8; detail is cut to 500 bytes on a
 * character boundary. */
typedef struct {
    uint64_t generation;
    const char *state;               /* one of control.STATES */
    const char *detail;
    const yusb_target_t *target;     /* NULL: null */
    const char *udc;
    const char *link_speed;
    const yctl_pair_t *device;       /* vendor, product, remote_speed, presented_speed */
    size_t device_count;
    const yctl_counter_t *counters;
    size_t counter_count;
    uint32_t reconnects;
    double updated;                  /* wall-clock seconds, like time.time() */
} yctl_status_t;

/* control.encode({"ok": true, "status": ...}): sorted keys at every level,
 * compact separators, ASCII-only escaping, trailing '\n'. Returns the
 * length written (NUL terminated), or 0 if it would exceed cap or
 * YCTL_MAX_MESSAGE. */
size_t yctl_format_status(const yctl_status_t *status, char *out, size_t cap);
/* {"error": error[:200], "ok": false} plus '\n'; 0 if it does not fit. */
size_t yctl_format_error(const char *error, char *out, size_t cap);
