# usbtrace: usbmon capture, trace format and lever replay

Evidence tooling for ADR 0028 (`doc/decisions/0028-negotiated-usbip-payload-extension.md`,
§9): capture real device traffic with Linux `usbmon`, generate synthetic traces, and
replay either through a USB/IP wire model to see what each lever would buy over
100 Mbit and 1 Gbit Ethernet. **Replay results are analytic upper bounds from a
model, software-only; not hardware.**

| File | Purpose | Runs on |
|---|---|---|
| `capture.py` | usbmon to pcap (link type 220) via `MON_IOCX_GETX` | board, root, python3 >= 3.9 |
| `pcapfmt.py` | classic pcap and usbmon record codec | anywhere (stdlib) |
| `urbs.py` | S/C pairing into transfers; BOT CBW/CSW grouping | anywhere |
| `synth.py` | deterministic synthetic traces | anywhere |
| `yxenc.py`, `yxshim.c` | builds and loads the real C encoder (`native/yusb/src/yx.c`) | host with `cc` |
| `replay.py` | the lever benchmark (JSON or Markdown) | host with `cc` |

All modules are stdlib only and avoid Python 3.10+ features, so they also run with
the board's system `python3`.

## Capture on the Radxa ZERO 3E

Only `capture.py` and `pcapfmt.py` are needed on the board.

```sh
# On the workstation, from the repository root:
scp tools/usbtrace/capture.py tools/usbtrace/pcapfmt.py root@zero3e:/root/

# On the board, as root:
modprobe usbmon
lsusb            # "Bus 003 Device 004: ID 0781:5581 ..." -> --bus 3 --devnum 4
lsusb -t         # driver (usb-storage, usbhid, snd-usb-audio) and speed per port
python3 /root/capture.py --bus 3 --devnum 4 --duration 120 --max-bytes 2G \
    --out /root/disk-read.pcap
```

While it runs (Ctrl-C stops early and still closes the file), exercise the device
from a second shell, for example:

```sh
dd if=/dev/sda of=/dev/null bs=64k count=4096 iflag=direct   # raw sequential read
mount /dev/sda1 /mnt && tar cf - /mnt | cat >/dev/null        # file reads
cp -r /usr/share/doc /mnt/doc-copy && sync && umount /mnt     # file writes
```

Then copy the trace back (never commit captures; they hold the device's data):

```sh
mkdir -p .scratch/usbtrace/captures
scp root@zero3e:/root/disk-read.pcap .scratch/usbtrace/captures/
```

The device address changes when the device is re-plugged; re-check `lsusb`.
`capture.py` exits 0 on success, 1 on an open or read error (with a `modprobe`
or root hint) or when usbmon rejects every ring size, 2 on bad options, and 3 when the kernel dropped events (enlarge
`--ring-size`). It prints records written, events filtered by `--devnum`,
events truncated and kernel drops.

Options: `--bus N` opens `/dev/usbmonN` (0 = all buses), `--devnum N` keeps one
device, `--duration S`, `--max-bytes 2G` (the file never exceeds it),
`--ring-size 32M` (8K..64M), `--snaplen 0` (bytes kept per event; 0 keeps everything).

**Memory and completeness.** The usbmon ring is unswappable kernel memory, one per
open reader. Current mainline kernels accept 8 KiB to 64 MiB; older and vendor (BSP)
kernels only up to 1200 KiB, and both answer a larger size with `EINVAL`. usbmon
keeps at most one fifth of the ring as data per event, so the kernel default
(300 KiB) truncates every 64 KiB URB to 60 KiB. `capture.py` asks for 32 MiB by
default (6.4 MiB per event, about 0.8 s of buffering for a 40 MB/s stream). When
the kernel rejects the size with `EINVAL` it retries once at 1200 KiB (240 KiB per
event, still a whole 64 KiB URB, but about 30 ms of buffering, so watch the drop
count) and prints the size it got: `usbmon ring: requested N, kernel uses M`, then
`capturing ... ring=M`. If 1200 KiB is rejected too it exits 1 with
`cannot size the /dev/usbmonN ring`; try a smaller `--ring-size`. Each event
carries at most `len_cap` bytes. usbmon cannot copy some buffers at all
(`flag_data` `D` for scatter-gather lists the IOMMU merged, `Z` for no buffer):
such events have no data. Replay reports any shortfall as `captured_fraction`
below 1 and flags the trace as truncated.

**Equivalent tools** produce the same link type 220 records:

```sh
tcpdump -i usbmon3 -s 0 -B 65536 -w disk.pcap   # -B: a larger buffer, in KiB; libpcap
                                                # keeps at most 262144 bytes per event
dumpcap -i usbmon3 -P -s 0 -w disk.pcap         # -P writes classic pcap, not pcapng
editcap -F pcap in.pcapng out.pcap              # convert an existing pcapng
```

Neither filters by device address; use `replay.py --devnum N` instead.

### ioctl numbers (generic `_IOC` encoding: x86, arm, arm64)

`MON_IOC_MAGIC` is 0x92. `dir << 30 | size << 16 | type << 8 | nr`.

| ioctl | Definition | Value (64-bit) |
|---|---|---|
| `MON_IOCG_STATS` | `_IOR(0x92, 3, struct mon_bin_stats)` (8 bytes: queued, dropped) | `0x80089203` |
| `MON_IOCT_RING_SIZE` | `_IO(0x92, 4)`, argument is the size | `0x9204` |
| `MON_IOCQ_RING_SIZE` | `_IO(0x92, 5)`, returns the size | `0x9205` |
| `MON_IOCX_GETX` | `_IOW(0x92, 10, struct mon_bin_get)` (hdr, data pointers, `size_t alloc`) | `0x4018920A` (`0x400C920A` on 32-bit) |

The device is opened with `O_NONBLOCK` and `poll()`ed every 200 ms, so SIGINT,
SIGTERM and `--duration` take effect even when the bus is idle. `MON_IOCG_STATS`
reads the drop count once at the end.

## Trace format

A trace is a **classic pcap** file (version 2.4; magic `a1b2c3d4` for microsecond
or `a1b23c4d` for nanosecond timestamps, in either byte order) with link type
**220** (`LINKTYPE_USB_LINUX_MMAPPED`, 64-byte header) or **189**
(`LINKTYPE_USB_LINUX`, the first 48 bytes). pcapng is rejected with a conversion
hint. The usbmon header uses the byte order of the pcap header (the capturing
host's), as libpcap writes it.

Each record is one usbmon event: `struct usbmon_packet`, then the data area of
`len_cap` bytes. For link type 220 the data area starts with `ndesc` isochronous
descriptors of 16 bytes (`int status; u32 offset; u32 length; u32 pad`), then the
payload. The record's original length is 64 + `len_cap`; a captured length below
it means `--snaplen` truncated the record (the header is left as usbmon wrote it).

| Offset | Field | Meaning | Used by replay |
|---|---|---|---|
| 0 | `u64 id` | URB id (kernel address), reused after completion | pairing key with bus |
| 8 | `char type` | `S` submit, `C` complete, `E` submission error | pairing |
| 9 | `u8 xfer_type` | usbmon: 0 isochronous, 1 interrupt, 2 control, 3 bulk | eligibility, RTT rules |
| 10 | `u8 epnum` | endpoint, 0x80 set for IN | direction |
| 11 | `u8 devnum` | device address | `--devnum`, per-device state |
| 12 | `u16 busnum` | bus | `--bus`, pairing key |
| 14 | `char flag_setup` | 0 when `setup` holds a control setup packet | control setup |
| 15 | `char flag_data` | 0 when data follows; `<` `>` none for this phase; `D` `Z` unavailable | — |
| 16 | `s64 ts_sec`, 24 `s32 ts_usec` | event time | device time |
| 28 | `int status` | `-EINPROGRESS` on S; URB status on C/E | outcome |
| 32 | `u32 length` | S: buffer length; C: actual length | payload length |
| 36 | `u32 len_cap` | data area bytes delivered (descriptors included) | captured bytes |
| 40 | `u8 setup[8]` | control setup, or iso `error_count`, `numdesc` | — |
| 48 | `int interval`, 52 `int start_frame`, 56 `u32 xfer_flags`, 60 `u32 ndesc` | link type 220 only | iso descriptors |

**Pairing.** An `S` event opens a transfer keyed by `(busnum, id)`; the next `C`
or `E` with that key closes it. OUT payload is the `S` event's data (`length` is
the buffer length, which USB/IP sends in `CMD_SUBMIT`); IN payload is the `C`
event's data (`length` is the actual length, sent in `RET_SUBMIT`). A second `S`
with an open key counts the first as unmatched; a `C` without an `S` (submitted
before the capture started) is an orphan; transfers still open at the end are
unmatched. Outcomes: status 0 `ok`; `E` `submit-error`; `-ENOENT`/`-ECONNRESET`
`unlinked` (killed or unlinked URBs, possibly with partial data); `-ESHUTDOWN`/
`-ENODEV` `gone`; anything else (`-EPIPE` stall, `-EPROTO`, ...) `error`.

**BOT commands.** Per device, a bulk OUT transfer whose data is a valid 31-byte
CBW (`USBC`) opens a command. Later bulk transfers on that device are its data
phase until a 13-byte CSW (`USBS`) with the same tag closes it. READ(10) and
WRITE(10) LBAs and lengths come from the CDB.

## Synthetic traces

```sh
.scratch/venv/bin/python tools/usbtrace/synth.py              # -> .scratch/usbtrace/*.pcap
.scratch/venv/bin/python tools/usbtrace/synth.py --scale 2 --seed 7 --only disk-zero
```

Output depends only on `--seed` and `--scale` (scale 1 = 16 MiB per disk class,
about 55 MB of files in total, generated in under a second). Timestamps come from
a synthetic clock: bulk data at the 40 MB/s high-speed ceiling, plus 100 µs per
read and 250 µs per write command.

| Class | Conversation |
|---|---|
| `disk-zero` | BOT enumeration, INQUIRY, TEST UNIT READY failing with UNIT ATTENTION, REQUEST SENSE, READ CAPACITY, MODE SENSE, mount-like 4 KiB reads, then 64 KiB READ(10)s over a filesystem image: per 2 MiB group about 70% zero blocks, 12% metadata (bitmaps, ext4-like inodes) and 18% file data (text, executable-like, random) |
| `files-text` | 128 KiB WRITE(10)s (two 64 KiB URBs) of log, C-source and prose files with an inode write after each, reading them back with 64 KiB READ(10)s, then a hot set of up to four files read three times. The re-reads are the only source of CACHE hits and model nothing measured |
| `media-jpeg` | 64 KiB READ(10)s of JFIF files with random, byte-stuffed scan data and an MP4 with a random `mdat`, each after a 4 KiB directory/FAT read |
| `hid` | keyboard (8-byte) and mouse (4-byte) interrupt IN reports after enumeration, SET_IDLE and report descriptor reads: typing at about 3–8 keys/s, 125 Hz motion bursts over 20 s, ended by unlinks (`-ENOENT`) |
| `audio-iso` | UAC1 headset at full speed: SET_INTERFACE, SET_CUR sampling rate, then isochronous OUT (192 B/ms) and IN (96 B/ms) URBs of 8 packets with descriptors. Yoke does not support isochronous transfers (ADR 0021), so replay reports it as not eligible |

## Replay benchmark

```sh
.scratch/venv/bin/python tools/usbtrace/replay.py --markdown            # all of .scratch/usbtrace
.scratch/venv/bin/python tools/usbtrace/replay.py .scratch/usbtrace/captures/disk-read.pcap \
    --devnum 4 --rtt-ms 0.2 --rtt-ms 1 --json-out .scratch/usbtrace/disk-read.json
```

JSON goes to stdout (or a table with `--markdown`; `--json-out` saves the JSON
too). Every number is an **analytic upper bound** (`"estimate": "analytic upper
bound"` in the JSON). Each trace reports counts, outcomes, payload bytes, the
eligible fraction, `captured_fraction` (captured payload bytes over URB lengths)
with `captured_bytes`, `uncaptured_bytes`, `truncated` and `truncated_transfers`,
and per configuration: the `tiers` that can use it (`exporter` `any` or `yusb`,
and `usable_on` per link), payload and wire bytes (each way),
`ratio` (payload / wire), `gain_vs_plain` (plain wire / wire), encoder block
counts, encode time per MiB, round trips, read-ahead hits and misses, and
throughput with its bound (link or device) per link and RTT. The first build
compiles the encoder into `.scratch/cache/usbtrace/` (see `yxenc.py`); without
`native/yusb/src/yx.c` the YX rows say "encoder unavailable". Every encoded
payload is decoded again and compared unless `--no-verify`.

Configurations: `plain`, `write-behind`, `read-ahead`, `FILL`, `FILL+LZ4`,
`FILL+LZ4+CACHE` (256 blocks, `--cache-blocks`), and `all` (every lever).
`--max-urb N` sets the bulk URB split (default 32768, the native worker's
`max_transfer`; 65536 models the Python worker).

**Tiers** (ADR 0027, 0028). A row is labelled with the tiers whose device-side
exporter can use it. YX levers and read-ahead need a yusb exporter: tier 1
(ESP32-S3), tier 1.5 (ESP32-P4), or a future native yusb exporter on Linux.
Tier 2 (ZERO 3E) runs the kernel's stock `usbip-host`, which speaks plain
USB/IP only, so only `plain` and `write-behind` (a computer-side proxy lever)
apply to it. Tier 1 is capped near 1 MB/s by full-speed USB, so it is dropped
from every row whose 100M figures exceed that (`FULL_SPEED_MBPS`); there the
100M columns describe tier 1.5. The 1G columns stand for tier 2 and the future
native exporter; in the Markdown a `*` marks a 1G figure
that only that future exporter could reach.

**Truncated captures.** Plain rows count every URB byte, captured or not, so
they are exact under the model. YX rows encode only the captured bytes; they
neither encode the uncaptured eligible bytes nor count them as RAW, but
**scale** them by the wire/data ratio the encoder reached on the captured bytes
of the same device and direction (plain size when that stream has none), and
report them separately per row: `uncaptured_bytes`,
`uncaptured_wire_estimate`, `uncaptured_unscaled_bytes`. The Markdown shows
`TRUNCATED` in the Captured column and lists each truncated trace below the
table. Recapture with a larger ring rather than trusting scaled rows.

### Model

- **Wire.** Each URB costs a 48-byte USB/IP header each way (`CMD_SUBMIT`,
  `RET_SUBMIT`) plus OUT data in `CMD_SUBMIT` or IN data in `RET_SUBMIT`; an
  unlinked URB adds `CMD_UNLINK` and `RET_UNLINK`. Bulk transfers are split into
  `--max-urb` URBs (32 KiB, the native worker's maximum). With YX levers,
  eligible payloads (bulk, at least 512 bytes; never control or interrupt)
  become the real C encoder's block stream, one encoder per device and
  direction, in trace order. Uncaptured bytes are scaled, as above.
- **Ethernet.** Each message is cut into 1448-byte segments (1500 MTU, TCP
  timestamps). Each segment costs 90 more bytes: 20 IP + 20 TCP + 12 option + 14
  MAC + 4 FCS + 8 preamble + 12 inter-frame gap. Every second segment draws a
  90-byte ACK the other way. Plain payload efficiency is therefore about 0.90.
- **Link time** = wire bits / link rate, summing per URB the busier direction. BOT is
  sequential, so reads and writes do not overlap; ACKs ride the idle direction.
- **Device time** = the union of the trace's submit-to-complete intervals, but at
  least bulk bytes over a 40 MB/s high-speed ceiling (`--bus-ceiling-mbps`). The
  computer-facing gadget is high speed in every tier, so the ceiling also holds
  for traces captured from faster devices.
- **Elapsed** = max(link time, device time) + RTT × unhidden round trips; RTTs
  0.2 ms (LAN) and 1 ms by default. Plain BOT pays one round trip per command
  (CBW..CSW) and one per data-out URB (window 1). Write-behind removes the
  data-out ones. Read-ahead (ADR 0028 §8 semantics) hides min(RTT, the command's
  device time) for a READ(10) that exactly continues a good READ(10) (same LUN,
  next LBA, same length); any other command after a good READ(10) waits
  max(0, that read's device time − RTT) for the discarded speculation. Control
  transfers pay one round trip each, other OUT URBs one each, IN pumps none.
- **Throughput** = payload bytes / elapsed, in MB/s (10^6 bytes/s).
- **Encode CPU.** Measured time inside the C encoder on this host, per MiB
  encoded. The Cortex-A55 column multiplies it by 6, an **inference**: about 3×
  clock (desktop x86 near 5.5 GHz vs the 1.8 GHz RK3566) times about 2× per clock
  (wide out-of-order core vs in-order dual-issue A55). Replace it with a
  measurement by running replay on the board (`--a55-factor 1` there):

  ```sh
  # workstation, repository root: copy the tool and the encoder sources, keeping paths
  rsync -aR tools/usbtrace native/yusb native/third_party/lz4 root@zero3e:/root/yoke/
  # board (needs cc): synthetic traces, then the YX configurations only
  cd /root/yoke && python3 tools/usbtrace/synth.py --scale 0.5 &&
    python3 tools/usbtrace/replay.py --markdown --a55-factor 1 \
      --config FILL --config FILL+LZ4 --config FILL+LZ4+CACHE
  ```

### Results on the synthetic traces

`make benchmark-usbtrace` (`synth.py` seed 1, scale 1; `replay.py`, default
`--max-urb 32768`), 2026-11-05, Intel Core Ultra 9 285K, Python 3.14, `yx.c` as
of that date. Analytic upper bounds from the replay model, software-only; not
hardware. Throughput in MB/s; A55 is the ×6 inference. Eligible is 1.00 for the
disk traces and 0 for HID; captured fraction is 1.00 for every synthetic trace;
`media-jpeg` rows equal to plain are omitted. `*`: no existing tier can use this
figure (YX or read-ahead on GbE needs the future native exporter; tier 2 is
plain or write-behind only). Tier 1 (full speed, about 1 MB/s) appears only on
the HID rows; the disk rows' 100M figures describe tier 1.5.

| Trace | Config | Tiers | Payload MiB | Wire MiB | Ratio | Enc ms/MiB | A55 ms/MiB | 100M 0.2ms | 100M 1ms | 1G 0.2ms | 1G 1ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| disk-zero | plain | 1.5, 2, native | 16.03 | 17.77 | 0.90 | - | - | 11.3 | 9.8 | 32.9 | 23.0 |
| disk-zero | write-behind | 1.5, 2, native | 16.03 | 17.77 | 0.90 | - | - | 11.3 | 9.8 | 32.9 | 23.0 |
| disk-zero | read-ahead | 1.5, native | 16.03 | 17.77 | 0.90 | - | - | 11.7 | 11.5 | 36.6* | 35.4* |
| disk-zero | FILL | 1.5, native | 16.03 | 5.45 | 2.94 | 0.06 | 0.38 | 32.9 | 23.0 | 32.9* | 23.0* |
| disk-zero | FILL+LZ4 | 1.5, native | 16.03 | 4.09 | 3.92 | 0.12 | 0.73 | 32.9 | 23.0 | 32.9* | 23.0* |
| disk-zero | FILL+LZ4+CACHE | 1.5, native | 16.03 | 4.08 | 3.93 | 0.17 | 1.03 | 32.9 | 23.0 | 32.9* | 23.0* |
| disk-zero | all | 1.5, native | 16.03 | 4.08 | 3.93 | 0.17 | 1.03 | 36.6 | 35.4 | 36.6* | 35.4* |
| files-text | plain | 1.5, 2, native | 17.20 | 19.05 | 0.90 | - | - | 10.9 | 8.6 | 29.7 | 17.3 |
| files-text | write-behind | 1.5, 2, native | 17.20 | 19.05 | 0.90 | - | - | 11.3 | 10.0 | 32.8 | 23.7 |
| files-text | read-ahead | 1.5, native | 17.20 | 19.05 | 0.90 | - | - | 10.7 | 9.0 | 28.0* | 18.9* |
| files-text | FILL | 1.5, native | 17.20 | 19.06 | 0.90 | 0.03 | 0.19 | 10.9 | 8.6 | 29.7* | 17.3* |
| files-text | FILL+LZ4 | 1.5, native | 17.20 | 10.10 | 1.70 | 1.37 | 8.24 | 19.6 | 13.3 | 29.7* | 17.3* |
| files-text | FILL+LZ4+CACHE | 1.5, native | 17.20 | 9.69 | 1.77 | 1.45 | 8.71 | 20.3 | 13.6 | 29.7* | 17.3* |
| files-text | all | 1.5, native | 17.20 | 9.69 | 1.77 | 1.45 | 8.71 | 20.8 | 18.9 | 30.7* | 26.7* |
| media-jpeg | plain | 1.5, 2, native | 16.04 | 17.77 | 0.90 | - | - | 11.2 | 9.8 | 32.9 | 22.9 |
| media-jpeg | read-ahead | 1.5, native | 16.04 | 17.77 | 0.90 | - | - | 11.5 | 11.4 | 35.4* | 34.1* |
| media-jpeg | FILL | 1.5, native | 16.04 | 17.79 | 0.90 | 0.03 | 0.19 | 11.2 | 9.8 | 32.9* | 22.9* |
| media-jpeg | FILL+LZ4 | 1.5, native | 16.04 | 17.79 | 0.90 | 0.03 | 0.19 | 11.2 | 9.8 | 32.9* | 22.9* |
| media-jpeg | FILL+LZ4+CACHE | 1.5, native | 16.04 | 17.79 | 0.90 | 0.14 | 0.83 | 11.2 | 9.8 | 32.9* | 22.9* |
| media-jpeg | all | 1.5, native | 16.04 | 17.79 | 0.90 | 0.14 | 0.83 | 11.5 | 11.4 | 35.4* | 34.1* |
| hid | every config | see row tiers | 0.005 | 0.28 | 0.02 | - | - | 0.00026 | 0.00026 | 0.00026 | 0.00026 |
| audio-iso | — | — | not eligible / unsupported (isochronous, ADR 0021) | | | | | | | | |

Readings: compression pays only where the link bounds (100 Mbit, tiers 1 and
1.5) and the data compresses; at 1 Gbit every disk trace is device-bound and only
the latency levers move, and of those tier 2 can use only write-behind.
Read-ahead loses on `files-text` at 0.2 ms: the speculation repeats the previous
length, so each file's short tail read misses and waits for a
discarded 64 KiB read (about 1.5 ms), more than the 0.2 ms a hit saves. HID
payloads are never encoded; its throughput is just its report rate.

### Limits of the model

- It is an analytic upper bound: device, network and the computer-side gadget
  are assumed to overlap perfectly. The current proxy forwards each BOT data phase as one store-and-forward
  64 KiB URB, so a plain read command costs closer to RTT + device + link +
  gadget time (about 4 ms for 64 KiB at 1 Gbit, about 16 MB/s, vs 32.9 here).
  Under that serial view read-ahead hides the whole device read, not just the RTT,
  and cut-through forwarding would matter more than any YX lever on 1 Gbit.
- Host think time, TCP slow start, congestion, Nagle (assumed off), interrupt
  and kernel scheduling are not modelled; nor are encoder CPU limits (the A55
  number is reported, not applied) or decode cost.
- Synthetic timestamps follow the 40 MB/s ceiling by construction, so synthetic
  device time is not evidence. The synthetic data mixes are assumptions; only
  captures decide the defaults (ADR 0028 §9).
- Write-behind is modelled with an unbounded window and no late STALLs;
  read-ahead with ADR 0028 §8 exact-match semantics.
- Isochronous transfers are excluded, as Yoke does not carry them; a trace whose
  isochronous payload exceeds the rest is reported unsupported.

## Tests

```sh
.scratch/venv/bin/python -m pytest tests/native_usb/test_native_usbtrace.py
.scratch/venv/bin/ruff format --check tools/usbtrace tests/native_usb/test_native_usbtrace.py
.scratch/venv/bin/ruff check tools/usbtrace tests/native_usb/test_native_usbtrace.py
.scratch/venv/bin/mypy --strict tools/usbtrace/*.py tests/native_usb/test_native_usbtrace.py
```

mypy needs the test and the tool modules in one invocation: the test imports the
tools by inserting `tools/usbtrace` into `sys.path`. The capture ioctl layer runs
against a fake `ioctl` and a regular file standing in for `/dev/usbmonN` (no
root), which covers the `EINVAL` retry at 1200 KiB.
