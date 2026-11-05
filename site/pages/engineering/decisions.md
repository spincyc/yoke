# Architecture decisions

These records preserve the reasoning behind Yoke's design. Read a record's
status and superseding decisions before applying an older choice.
The [architecture](../../../doc/design/ARCHITECTURE.md) describes the
current implementation; the [wire protocol](../../../doc/design/PROTOCOL.md)
is normative for control behavior.

| Record | Decision |
|---|---|
| [0001 — Standalone repository named yoke](../../../doc/decisions/0001-standalone-repository-named-yoke.md) |  |
| [0002 — Yoke is a control plane; the media plane is endpoint hardware](../../../doc/decisions/0002-control-plane-only.md) |  |
| [0003 — Controllerless pairing](../../../doc/decisions/0003-controllerless-pairing.md) | consumer-owned state, versioned intents |
| [0004 — HDCP position](../../../doc/decisions/0004-hdcp-position.md) | endpoint hardware's concern, never yoke's |
| [0005 — Control-frame authentication](../../../doc/decisions/0005-hmac-psk-authentication.md) | HMAC-SHA256 pre-shared key |
| [0006 — Sans-I/O protocol core with a simulated network](../../../doc/decisions/0006-sans-io-core.md) |  |
| [0007 — Sequence-number resumption uses Unix time as the floor](../../../doc/decisions/0007-seq-resume-via-unix-time.md) |  |
| [0008 — Implementation language](../../../doc/decisions/0008-python-stdlib-runtime.md) | Python, stdlib-only at runtime |
| [0009 — Anti-entropy re-issues intents; rejected intents are evicted](../../../doc/decisions/0009-reissued-anti-entropy.md) |  |
| [0010 — Denial-of-service hardening of the frame and node layers](../../../doc/decisions/0010-denial-of-service-hardening.md) |  |
| [0011 — Media and HDCP invariants](../../../doc/decisions/0011-media-and-hdcp-scope-for-yoke-endpoints.md) | scope for yoke-designed endpoints |
| [0012 — Durable transition feedback and reserved outbound sequences](../../../doc/decisions/0012-durable-core-transitions.md) |  |
| [0013 — Serialized driver effects and explicit agent lifecycle](../../../doc/decisions/0013-serialized-agent-effects.md) |  |
| [0014 — Reconcile only owned Linux USB/IP resources](../../../doc/decisions/0014-usbip-owned-reconciliation.md) |  |
| [0015 — Reference media multicast, passive debugging, and input bounds](../../../doc/decisions/0015-reference-media-multicast-and-input-bounds.md) |  |
| [0016 — Explicit provisioning and validated control inputs](../../../doc/decisions/0016-explicit-provisioning-and-control-inputs.md) |  |
| [0017 — Retained driver ownership and bounded teardown](../../../doc/decisions/0017-driver-teardown-and-status-evidence.md) |  |
| [0018 — Operator-controlled Linux release updates over Ethernet](../../../doc/decisions/0018-linux-operator-release-updates.md) |  |
| [0019 — ESP32 local Ethernet firmware updates](../../../doc/decisions/0019-esp32-local-ethernet-firmware-updates.md) |  |
| [ADR 0020](../../../doc/decisions/0020-optional-read-only-telemetry.md) | Optional read-only telemetry and terminal monitoring |
| [0021 — Userspace USB/IP client with gadget presentation](../../../doc/decisions/0021-userspace-usbip-client-and-raw-gadget-presentation.md) | computer-side USB data path |
| [0022 — Gadget worker process, metadata driver and simulation evidence](../../../doc/decisions/0022-gadget-worker-process-and-metadata-driver.md) |  |
| [0023 — Clock-robust admission, bounded sequence floor and transmitted-byte authentication](../../../doc/decisions/0023-clock-robust-admission-and-transmitted-byte-authentication.md) |  |
| [0024 — USB/IP data-plane admission and ZERO 3E device-box privileges](../../../doc/decisions/0024-usbip-data-plane-admission-and-device-box-privileges.md) |  |
| [0025 — IR endpoint on ESP32 RMT with an iTach API subset](../../../doc/decisions/0025-esp32-rmt-ir-endpoint-itach-subset.md) |  |
| [0026 — ESP32 endpoint health, recovery, storage and boot-security path](../../../doc/decisions/0026-esp32-endpoint-health-recovery-and-storage.md) |  |
| [0027 — Tiered USB product line and a native C data path](../../../doc/decisions/0027-tiered-usb-product-line-and-native-c-data-path.md) | tiers 1, 1.5, 2 and 3 |
| [0028 — Negotiated USB/IP payload extension (YX) and latency hiding](../../../doc/decisions/0028-negotiated-usbip-payload-extension.md) |  |
| [0029 — Native YWP/1 core: number model, replay digest and bounded tables](../../../doc/decisions/0029-native-ywp1-core-interpretations.md) |  |

Protocol behavior changes require updating the normative protocol and
recording the decision before implementation. See
[Contributing](../../../CONTRIBUTING.md) for the change workflow.
