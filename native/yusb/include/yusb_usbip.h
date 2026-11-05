#pragma once
/* USB/IP 1.1.1 wire codec (reference: src/yoke/usb/usbip.py).
 *
 * Normative: https://docs.kernel.org/usb/usbip_protocol.html. Every field
 * is big endian. The 48-byte usbip_header is shared by CMD_SUBMIT,
 * RET_SUBMIT, CMD_UNLINK and RET_UNLINK; payload follows separately.
 * Isochronous transfers are unsupported (ADR 0021). Pure; no allocation.
 */
#include "yusb.h"

#define YUSB_USBIP_VERSION 0x0111u

#define YUSB_OP_REQ_DEVLIST 0x8005u
#define YUSB_OP_REP_DEVLIST 0x0005u
#define YUSB_OP_REQ_IMPORT 0x8003u
#define YUSB_OP_REP_IMPORT 0x0003u

#define YUSB_CMD_SUBMIT 0x00000001u
#define YUSB_CMD_UNLINK 0x00000002u
#define YUSB_RET_SUBMIT 0x00000003u
#define YUSB_RET_UNLINK 0x00000004u

#define YUSB_USBIP_DIR_OUT 0u
#define YUSB_USBIP_DIR_IN 1u

/* include/uapi/linux/usbip.h transfer flags */
#define YUSB_URB_SHORT_NOT_OK 0x0001u
#define YUSB_URB_ZERO_PACKET 0x0040u
#define YUSB_URB_DIR_IN 0x0200u

#define YUSB_NON_ISO_PACKETS 0xffffffffu /* number_of_packets of a non-ISO URB */

#define YUSB_USBIP_HEADER_SIZE 48u
#define YUSB_OP_HEADER_SIZE 8u
#define YUSB_DEVICE_INFO_SIZE 312u
#define YUSB_INTERFACE_INFO_SIZE 4u
#define YUSB_BUSID_SIZE 32u
#define YUSB_PATH_SIZE 256u
#define YUSB_REQ_IMPORT_SIZE (YUSB_OP_HEADER_SIZE + YUSB_BUSID_SIZE)
#define YUSB_REP_IMPORT_SIZE (YUSB_OP_HEADER_SIZE + YUSB_DEVICE_INFO_SIZE)
/* Bound on accepted transfer_buffer_length/actual_length (memory safety). */
#define YUSB_MAX_TRANSFER (16u * 1024u * 1024u)

/* Linux errno values carried in RET_SUBMIT/RET_UNLINK status. They are wire
 * values (asm-generic numbering), not the local platform's errno. */
#define YUSB_EPERM 1
#define YUSB_ENOENT 2
#define YUSB_EINTR 4
#define YUSB_EIO 5
#define YUSB_ENOMEM 12
#define YUSB_EBUSY 16
#define YUSB_ENODEV 19
#define YUSB_EINVAL 22
#define YUSB_EPIPE 32
#define YUSB_ETIME 62
#define YUSB_EPROTO 71
#define YUSB_EOVERFLOW 75
#define YUSB_EILSEQ 84
#define YUSB_ECONNRESET 104
#define YUSB_ESHUTDOWN 108
#define YUSB_ETIMEDOUT 110
#define YUSB_EREMOTEIO 121

/* One decoded usbip_header. Fields unused by `command` are zero. */
typedef struct {
    uint32_t command;
    uint32_t seqnum;
    uint32_t devid;
    uint32_t direction;
    uint32_t ep;
    /* CMD_SUBMIT */
    uint32_t flags;
    uint32_t length;            /* transfer_buffer_length, <= YUSB_MAX_TRANSFER */
    int32_t start_frame;        /* also RET_SUBMIT */
    uint32_t number_of_packets; /* also RET_SUBMIT */
    int32_t interval;
    uint8_t setup[8];
    /* RET_SUBMIT, RET_UNLINK */
    int32_t status;
    uint32_t actual_length;     /* <= YUSB_MAX_TRANSFER */
    int32_t error_count;
    /* CMD_UNLINK */
    uint32_t unlink_seqnum;
} yusb_usbip_hdr_t;

/* Decode one header with usbip.decode_header's checks: a known command,
 * CMD_SUBMIT direction 0/1 and ep <= 15, lengths within YUSB_MAX_TRANSFER.
 * Returns YUSB_OK or YUSB_ERR_PROTOCOL. Extension command codes negotiated
 * by ADR 0028 are decoded by yusb_yx.h, not here. */
int yusb_usbip_decode(const uint8_t raw[YUSB_USBIP_HEADER_SIZE], yusb_usbip_hdr_t *out);
/* Encode the fields `command` uses; everything else is written as zero.
 * RET_* replies carry the devid/direction/ep the caller set (stub: 0). */
void yusb_usbip_encode(const yusb_usbip_hdr_t *h, uint8_t raw[YUSB_USBIP_HEADER_SIZE]);

/* Convenience constructors for the four messages. */
yusb_usbip_hdr_t yusb_cmd_submit(uint32_t seqnum, uint32_t devid, unsigned direction,
                                 unsigned ep, uint32_t flags, uint32_t length,
                                 int32_t interval, const uint8_t setup[8]);
yusb_usbip_hdr_t yusb_ret_submit(uint32_t seqnum, int32_t status, uint32_t actual);
yusb_usbip_hdr_t yusb_cmd_unlink(uint32_t seqnum, uint32_t devid, uint32_t victim);
yusb_usbip_hdr_t yusb_ret_unlink(uint32_t seqnum, int32_t status);

/* usbip_usb_device as carried by OP_REP_IMPORT and OP_REP_DEVLIST. */
typedef struct {
    char path[YUSB_PATH_SIZE];
    char busid[YUSB_BUSID_SIZE];
    uint32_t busnum;
    uint32_t devnum;
    uint32_t speed;
    uint16_t vendor;
    uint16_t product;
    uint16_t bcd_device;
    uint8_t device_class;
    uint8_t device_subclass;
    uint8_t device_protocol;
    uint8_t configuration_value;
    uint8_t num_configurations;
    uint8_t num_interfaces;
} yusb_device_info_t;

typedef struct {
    uint8_t cls;
    uint8_t subclass;
    uint8_t protocol;
} yusb_iface_info_t;

/* The client-side devid: (busnum << 16) | devnum. */
static inline uint32_t yusb_devid(const yusb_device_info_t *info)
{
    return (info->busnum & 0xffffu) << 16 | (info->devnum & 0xffffu);
}

/* Strings must be NUL terminated inside their field and ASCII (bytes after
 * the NUL are ignored, as by every stock peer). YUSB_ERR_PROTOCOL if not. */
int yusb_device_info_decode(const uint8_t raw[YUSB_DEVICE_INFO_SIZE], yusb_device_info_t *out);
/* YUSB_ERR_RANGE if a string does not fit or is not ASCII. Pads with NUL. */
int yusb_device_info_encode(const yusb_device_info_t *info, uint8_t raw[YUSB_DEVICE_INFO_SIZE]);

/* Operation header (op_common): version, code, status. */
void yusb_op_encode(uint16_t code, uint32_t status, uint8_t raw[YUSB_OP_HEADER_SIZE]);
/* YUSB_ERR_PROTOCOL on a version other than 1.1.1. */
int yusb_op_decode(const uint8_t raw[YUSB_OP_HEADER_SIZE], uint16_t *code, uint32_t *status);

/* OP_REQ_IMPORT: op header + 32-byte busid field. */
int yusb_req_import_encode(const char *busid, uint8_t raw[YUSB_REQ_IMPORT_SIZE]);
/* Busid field to string; YUSB_ERR_PROTOCOL if unterminated or not ASCII. */
int yusb_busid_decode(const uint8_t field[YUSB_BUSID_SIZE], char busid[YUSB_BUSID_SIZE]);

/* OP_REP_DEVLIST: op header + u32 count, then per device the 312-byte
 * record and num_interfaces 4-byte (class, subclass, protocol, pad)
 * entries. */
size_t yusb_devlist_head_encode(uint32_t count, uint8_t raw[YUSB_OP_HEADER_SIZE + 4]);
int yusb_iface_info_encode(const yusb_iface_info_t *iface, uint8_t raw[YUSB_INTERFACE_INFO_SIZE]);
