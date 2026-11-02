# 0017 — Retained driver ownership and bounded teardown

Date: 2026-11-02. Status: accepted.

Idempotent detach means repeated successful teardown is harmless. It does
not mean a failed teardown may be suppressed. The stream driver formerly
forgot its process before waiting, then ignored a timeout. A replacement
could start while the previous pipeline still ran. The JAP driver similarly
reported idle after a failed channel-zero command.

Keep the owned resource until teardown is confirmed. Stop a stream with a
bounded terminate, wait, kill and wait sequence. A timeout or OS failure
retains the handle, reports `DriverError`/error status, and prevents a
replacement from starting. A later detach retries the same resource. The
production supervisor owns the process group on POSIX so pipeline children
receive teardown signals too; inability to drain the group is a failure,
not permission to start another pipeline. All driver I/O deadlines must be
finite and positive. Orderly runner shutdown drains real driver operations
(ADR 0013); Python task cancellation does not establish device teardown.

For JAP, preserve the requested channel after failed teardown and explicitly
query channel zero before declaring idle. The command vocabulary, channel
zero behavior and reply grammar remain isolated, bench-unverified firmware
assumptions. The Telnet adapter handles fragmented line replies and refuses
unsupported options according to RFC 854; an incomplete, ambiguous or
oversized reply is an error. It never treats the concatenation of unrelated
reply digits as a channel number. Every recognized channel line must be
valid and agree, including out-of-range or malformed lines accompanying an
otherwise valid channel-zero readback. The Telnet adapter passes all complete
lines already received together to this validation and finishes any known
partial trailing line, including whitespace-only prefixes, before returning.
Only an empty byte buffer establishes that no partial line is already known;
complete blank lines are consumed normally. It cannot establish that no further
line will arrive after an otherwise complete response: the firmware's reply
terminator/prompt and single-response-line assumption still require bench
verification. This is not a universal firmware grammar.

A channel readback that differs from the requested route is a retryable
driver error. It can indicate a decoder reset or a set command that was
acknowledged without taking effect; leaving it pending indefinitely does not
repair the route. Retrying uses the same confirmed teardown barrier before
reapplying the requested channel.

A configured JAP warden has exclusive authority over its dedicated decoder.
A fresh driver therefore starts with an unknown physical route, not confirmed
idle. Its first detach (including the restored core's startup teardown barrier)
must send channel zero and confirm zero through readback. The first attach
uses that same barrier. Only a confirmed detached decoder permits a later
detach to be a no-op. Failed zero commands/readbacks retain the unknown route
and report an error; neither restart nor replacement may bypass that cleanup.
This covers a crash after saving an unpaired intent but before driving the
appliance. It does not assume a process-local channel cache survives restart.

The dedicated warden retains decoder authority while unpaired: polling reads
back channel zero and treats a later nonzero channel (including appliance
reboot drift) as a retryable error. A stopped warden cannot keep enforcing that
state; channel-zero semantics still require physical bench verification.

The default external stream builder accepts only `framing="standard-rtp"`;
the reference sender advertises `framing="yoke-reference-v1"` (ADR 0015).
Encoding names alone cannot bridge the reference framing into a standard
depayloader. Dynamic video payload caps explicitly include typed 90000 Hz
clock rate and the verified encoding name. Caller-supplied names/rates must
agree; arbitrary cap fragments are never interpolated.

RAW requires explicit sampling, depth, width, height and colorimetry, using
the format subset supported by `rtpvrawdepay`. JPEG XS uses the documented
`rtpjxsvdepay` / `JXSV` mapping and explicit format metadata with packetmode
zero. Its configured `svtjpegxsdec` decoder limits the default path to YCbCr
4:4:4/4:2:2/4:2:0, depths 8/10/12, and dimensions 16 through 16384.
H.265 uses `rtph265depay` / `H265`. The old guessed JPEG XS element name
is removed. VC-2's RFC 8450 `vc2` metadata may be described, but the default
builder refuses it because this implementation has no verified GStreamer
depayloader mapping; a separately validated custom builder is required.
Generated commands are contract-checked offline, not a codec, plugin-availability,
display, interoperability or latency demonstration.

Primary cap references: [RTP jitterbuffer](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html),
[RAW depayloader](https://gstreamer.freedesktop.org/documentation/rtp/rtpvrawdepay.html)
and its [upstream caps implementation](https://github.com/GStreamer/gstreamer/blob/main/subprojects/gst-plugins-good/gst/rtp/gstrtpvrawdepay.c),
[JPEG XS depayloader](https://gstreamer.freedesktop.org/documentation/rsrtp/rtpjxsvdepay.html),
[JPEG XS decoder](https://gstreamer.freedesktop.org/documentation/svtjpegxs/svtjpegxsdec.html),
[H.265 depayloader](https://gstreamer.freedesktop.org/documentation/rtp/rtph265depay.html)
and [VC-2 RTP metadata](https://www.rfc-editor.org/rfc/rfc8450.html#section-7).

`active` describes the driver's attachment evidence, not a universal claim
that payload is flowing or an application has consumed it. IGMP membership,
pipeline process liveness and selected appliance channel are distinct
readiness evidence. Drivers describe that evidence and its limits in status
detail. No health check receives or inspects media bytes. Physical behavior
still requires the bench verification recorded in `HARDWARE.md`.
