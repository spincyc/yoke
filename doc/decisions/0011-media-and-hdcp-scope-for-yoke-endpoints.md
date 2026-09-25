# 0011 — Media and HDCP invariants: scope for yoke-designed endpoints

Date: 2026-09-24. Status: accepted (user, 2026-09-24). Amends ADR 0002
and ADR 0004; `AGENTS.md` carries the amended invariants.

## Context

ADRs 0002 and 0004 were written when yoke was only a control plane and
every endpoint was purchased. ADR 0004 says "no yoke component may ever
receive, hold, or relay media bytes or HDCP secrets"; `AGENTS.md` says no
component may participate in HDCP in any way. Three later designs
collide with that wording:

1. `yoke.media` (the software pipeline, runner, and snooper) receives and
   inspects payload.
2. The DSC link (`doc/design/EDGE-DEVICES.md`) is gateware that relays
   media bytes of unprotected content.
3. An **HDCP pass-through** link: a bridge that forwards HDCP-encrypted
   content as opaque ciphertext and tunnels the authentication messages
   between source and sink, so the ultimate endpoints negotiate HDCP
   directly and the bridge never decrypts. This is how HDMI extenders,
   fiber HDMI cables, and HDBaseT carry protected content.

The earlier research's reasons for ruling out a non-decrypting relay do
not hold on a LAN: HDCP 2.x's locality check allows 20 ms round trip on
HDMI, and a switched LAN is ~0.3 ms (`doc/research/FEASIBILITY.md`
§HDCP). The real limits are bandwidth (ciphertext cannot be compressed)
and exact timing, which are engineering, not legal, constraints.

## Decision

1. **The agent and control plane never touch media.** `yoke.core`,
   `yoke.net`, the drivers, the CLI, and the control-plane snooper never
   receive, inspect, or relay media payload. Unchanged from ADR 0002.
2. **Media code outside the agent may carry unprotected media.**
   `yoke.media` tooling and yoke-designed endpoint gateware may receive,
   inspect, transform, and forward the payload of unprotected content.
   They run outside the agent process or in endpoint hardware, and the
   agent drives them only through a driver.
3. **HDCP pass-through is permitted; participation is not.** A
   yoke-designed endpoint may forward HDCP-encrypted content as opaque
   ciphertext and tunnel HDCP authentication messages between the source
   and the sink verbatim. It must never:
   - decrypt protected content, or hold, derive, or store any HDCP key or
     session secret;
   - alter, synthesize, reorder, or selectively drop HDCP messages;
   - modify HDCP capability or version information seen by either side;
   - instruct a source or sink to disable or downgrade protection;
   - compress, transcode, scale, or store protected content;
   - deliver one protected session to more than one sink.

   It may detect encryption-enable signaling in the link framing, to
   select pass-through mode, and may synthesize EDID video-format
   capabilities (EDID carries no HDCP information).
4. **Licensed repeaters remain the path** for anything clause 3 forbids,
   notably one-to-many protected distribution. yoke drives purchased
   repeaters per ADR 0004.

The `AGENTS.md` invariants read: "the yoke agent and
control plane never receive or inspect media payload (ADRs 0002, 0011);
no component may decrypt HDCP content, hold HDCP secrets, alter HDCP
messages or capabilities, or instruct an endpoint to disable or
downgrade protection (ADRs 0004, 0011)."

## Consequences

- Protected content can cross the mesh on yoke-designed endpoints,
  strictly point-to-point, at full ciphertext rate. On 10G that is up to
  ~4K60 4:2:0 10-bit or 4K24 in any format; 4K60 4:2:2 12-bit needs 25G;
  4K120 needs 40G+.
- A pass-through endpoint needs raw link capture (FPGA transceivers),
  because merchant HDMI receiver chips are HDCP endpoints that decrypt or
  blank. It must regenerate identical timing, clock-locked, since the
  HDCP keystream is indexed by pixel position.
- **Legal reading, not a legal opinion:** a relay that never decrypts is
  not circumvention, and the HDCP license binds licensees implementing
  HDCP functions, which a pass-through does not. This is the same
  position as the extenders sold today. A commercial product needs this
  reviewed, and HDMI Adopter status regardless.
- The snooper sees ciphertext on protected links and verifies flow and
  framing only, as ADR 0004 already anticipates.

## Alternative considered

Keep yoke strictly a control plane: `yoke.media` becomes test and
tooling code only, and the DSC-link and pass-through endpoints become a
separate project with its own policy, driven by yoke like any purchased
endpoint. That keeps ADRs 0002 and 0004 verbatim and moves the same
questions into the endpoint project.

Chosen against because the user does not plan to sell anything
(2026-09-24): the alternative's one advantage, a clean licensing and
legal-review boundary for a product, does not apply, and splitting the
project adds coordination without adding safety — the lines that matter
(never decrypt, never hold keys, never weaken protection) are the same
either way.
