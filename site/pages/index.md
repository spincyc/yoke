# Connect endpoints.<br>Keep control *at the edge.*

<div class="hero-lead">A control plane for pairing HDMI and USB endpoints over switched Ethernet. Each endpoint owns its state; a passive observer can verify the conversation. No resident central controller.</div>

<div class="hero-actions"><a class="button" href="start.md">Start with the software →</a><a href="../../doc/design/ARCHITECTURE.md">Read the architecture →</a></div>

<figure class="network-diagram">
<svg viewBox="0 0 700 250" role="img" aria-labelledby="network-title network-description">
<title id="network-title">Endpoint-owned pairing over a switched network</title>
<desc id="network-description">A producer and consumer each have their own Yoke agent. Signed multicast intents travel through an Ethernet switch. A separate passive observer watches control traffic. Media travels between endpoints outside the control plane.</desc>
<path class="diagram-line" d="M181 96H280M420 96H519M350 129V179"/>
<rect class="diagram-box" x="20" y="46" width="161" height="100" rx="4"/>
<text class="diagram-text" x="39" y="78">Producer</text><text class="diagram-detail" x="39" y="100">source · local agent</text><text class="diagram-detail" x="39" y="124">owns its identity</text>
<rect class="diagram-core" x="280" y="63" width="140" height="66" rx="4"/>
<circle class="diagram-accent" cx="299" cy="84" r="3"/><circle class="diagram-accent" cx="309" cy="84" r="3"/>
<text class="diagram-core-text" x="350" y="108" text-anchor="middle">Ethernet switch</text>
<rect class="diagram-box" x="519" y="46" width="161" height="100" rx="4"/>
<text class="diagram-text" x="538" y="78">Consumer</text><text class="diagram-detail" x="538" y="100">sink · local agent</text><text class="diagram-detail" x="538" y="124">owns its pairing</text>
<rect class="diagram-box" x="255" y="179" width="190" height="51" rx="4"/>
<text class="diagram-text" x="275" y="201">Passive observer</text><text class="diagram-detail" x="275" y="218">observe · verify only</text>
<text class="diagram-detail" x="220" y="25">signed intents · endpoint state · no controller</text>
</svg>
<div class="diagram-mobile">
<div><strong>Producer</strong><span>source · local agent</span></div>
<div><strong>Consumer</strong><span>sink · local agent</span></div>
<p class="mobile-switch">↕ &nbsp; Signed control over switched Ethernet &nbsp; ↕</p>
<p class="mobile-observer"><strong>Passive observer</strong><span>Watch and verify. Transmit no application traffic.</span></p>
</div>
<figcaption>Control traffic carries intent and state. The endpoint media path stays separate.</figcaption>
</figure>

<div class="feature-grid">
<div class="feature"><span class="feature-number">01 / Ownership</span><h3>State lives at the edge.</h3><p>The CLI sends an intent and exits. Endpoints persist pairings and exchange state over authenticated multicast.</p></div>
<div class="feature"><span class="feature-number">02 / Observation</span><h3>Inspect control traffic.</h3><p>Announcements, acknowledgments, and events expose the control-plane conversation to an independent passive snooper.</p></div>
<div class="feature"><span class="feature-number">03 / Reproduction</span><h3>Bring failures offline.</h3><p>Pure state machines and seeded fault simulations turn loss, replay, partitions, and driver errors into repeatable tests.</p></div>
</div>

## Working software. An open hardware roadmap.

The repository implements a Python control plane, driver adapters, passive
observers, and a synthetic media reference pipeline. Runtime code uses only
the standard library. The media tooling exercises packetization, FEC, jitter,
and integrity; `jpegxs` and `vc2` select **ratio models**, not real codecs.

**Endpoint hardware has not been bench-tested.** The 4K120-over-10GbE
endpoint designs, DSC paths, USB compatibility, and physical latency goals
remain engineering proposals. Software simulation results establish model
behavior, not capture-to-display performance.

[Understand the verification boundary](verification.md) ·
[Explore the endpoint roadmap](../../doc/design/EDGE-DEVICES.md)

## A small command surface

```sh
yoke pair cam-garage tv-den
yoke unpair tv-den
yoke status
yoke snoop
```

A producer can feed a chosen consumer. Re-pairing changes the consumer's
route; discovery and reconciliation happen between endpoint agents. Hardware
behavior depends on the selected driver and endpoint.

[Start with an offline run](start.md) or read the
[normative wire protocol](../../doc/design/PROTOCOL.md).

## Follow the evidence

| Read | For |
|---|---|
| [Architecture](../../doc/design/ARCHITECTURE.md) | State ownership, sans-I/O boundaries, and driver placement |
| [Verification](verification.md) | Repeatable checks, seeded faults, and software benchmarks |
| [Media reference](../../doc/design/MEDIA.md) | The implemented software model and transport contract |
| [Research](../../doc/research/FEASIBILITY.md) | Dated feasibility analysis and unresolved hardware assumptions |
| [Contributing](../../CONTRIBUTING.md) | Setup, design changes, and useful fault reports |

<div class="search" data-doc-search>
<label for="doc-search">Find something in the docs</label>
<input id="doc-search" type="search" placeholder="Try persistence, multicast, or codec…" autocomplete="off" disabled>
<p class="search-status" role="status" aria-live="polite">Loading the local search index…</p>
<ul class="search-results"></ul>
<noscript><p>Use the documentation navigation or your browser’s find command.</p></noscript>
</div>
