# Security

Yoke is an early reference implementation. Its offline tests and loopback
checks do not constitute a security certification or a production hardware
assessment.

## Trust model

YWP/1 authenticates control frames with one site-wide HMAC-SHA256 key.
Anyone holding that key can author control traffic and impersonate another
origin. This is a shared trust domain, not per-device authorization. Sequence
and version checks limit replay and resolve ordering under the documented
persistence assumptions; state loss or a malicious key holder remains
outside that guarantee.

Control metadata is visible on the network. Pairing state, names, and
identities are not encrypted. Use a dedicated network and provision the
PSK through a private, authenticated channel. Open mode is for isolated
labs. See the [deployment guide](doc/design/DEPLOYMENT.md) and
[normative protocol](doc/design/PROTOCOL.md) for the exact behavior.

The software media reference uses synthetic, unprotected content. Its
integrity digests are test diagnostics, not sender authentication or
confidentiality. Do not treat its UDP media or feedback format as a secure
transport for an untrusted network. Endpoint and physical media security
require their own assessment.

## Reporting a vulnerability

Use the repository's **Security → Advisories → Report a vulnerability**
flow when private reporting is available. If that option is unavailable,
open an issue asking for a private reporting channel without publishing
exploit details, keys, credentials, or sensitive captures.

Include the revision, Python version, affected component, and a minimal
offline reproduction where possible. No response-time or supported-release
policy has been established yet; fixes currently target the development
branch.
