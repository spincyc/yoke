# 0004 — HDCP position: endpoint hardware's concern, never yoke's

Date: 2026-08-08. Status: accepted; scope amended by ADR 0011.

HDCP-protected HDMI cannot be captured, re-encoded, or forwarded by
unlicensed software without either breaking the law (stripping) or
breaking the picture (source refuses to play). Licensed AV-over-IP
endpoints (SDVoE, HDBaseT-over-IP, and licensed 1GbE appliance families)
carry HDCP hop-by-hop in certified silicon: the encoder terminates HDCP
with the source, the link is the vendor's protected transport, the
decoder re-establishes HDCP with the display.

Yoke's rule: **no yoke component may ever receive, hold, or relay media
bytes or HDCP secrets.** Yoke tells endpoints who feeds whom via their
public control interfaces. Whether a given source plays on a given
display is then strictly a property of the purchased endpoints. Hardware
selection (doc/research/HARDWARE.md) must prefer endpoints whose HDCP
handling is vendor-certified; unlicensed HDCP-stripping boxes are
excluded from recommendations.

The reference software media path (GStreamer/UVC on open endpoints) is
for unprotected sources — lab benches, cameras, our own computers — and
will simply show a blank/refused stream for protected content, which is
correct behavior, not a defect.
