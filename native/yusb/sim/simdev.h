#pragma once
/* A simulated high-speed Bulk-Only Transport mass-storage device behind the
 * export core's host-controller operations (yusb_hc_ops_t). Host-only test
 * and benchmark backend: packet-exact (512-byte bulk packets, short packet
 * or ZLP ends a data phase, the CSW is its own short packet), with SCSI
 * READ/WRITE(10), INQUIRY, sense and the probes Linux and Windows send.
 * The disk is a caller-provided memory region (a shared file mapping). */
#include "yusb_export.h"

typedef struct simdev simdev_t;

/* Completions are queued, never delivered re-entrantly: the server calls
 * simdev_drain() after each core call. */
simdev_t *simdev_create(uint8_t *disk, size_t size);
void simdev_destroy(simdev_t *d);
const yusb_hc_ops_t *simdev_ops(simdev_t *d);
void simdev_describe(const simdev_t *d, yusb_export_dev_t *dev, const char *busid);
/* Deliver queued completions to x; returns how many. */
unsigned simdev_drain(simdev_t *d, yusb_export_t *x, uint64_t now_ms);
