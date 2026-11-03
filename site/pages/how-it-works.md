<header class="product-intro"><p class="eyebrow">How it works</p><h1>One request. Two endpoints.<br>A saved connection.</h1>
<p class="hero-lead">Yoke separates choosing a connection from carrying its traffic. Agents remember who should connect; the endpoint backend carries video or USB data.</p></header>

<figure class="product-figure"><picture><source media="(max-width: 720px)" srcset="../assets/diagrams/control-and-payload-mobile.svg"><img src="../assets/diagrams/control-and-payload.svg" alt="A temporary control client sends pairing intent to local endpoint agents. HDMI travels separately from a computer through input hardware, an Ethernet switch, and output hardware to a display." loading="lazy"></picture><figcaption>Yoke chooses the route. The endpoint backend carries the traffic.</figcaption></figure>

## 1. Give each end an identity

A **producer** offers a source: a video stream or exported USB device.
A **consumer** uses one source: a display endpoint or a Linux USB/IP host.
Each has its own agent and durable state file.

The agent can run on an open Linux endpoint, or beside a closed appliance
as that device’s dedicated **warden**. Several wardens may share a
computer; each still owns only its own endpoint’s state.

## 2. Choose the source for a destination

```sh
yoke pair cam-garage tv-den
```

The command sends a signed request on the local network. The consumer
saves the new pairing, acknowledges it, then asks its driver to connect.
The CLI can exit as soon as the request has been accepted.

The acknowledgment confirms saved configuration. Use `yoke status` to see
the driver’s progress, and check the actual device to establish physical
output. These are separate steps with different evidence.

## 3. Let the endpoints keep the route

Agents announce their state and reconcile missed requests with peers.
After a restart, a consumer restores its saved pairing. If a source goes
offline, the consumer keeps that choice and reports `pending` while it
waits. Re-pairing or an explicit `unpair` changes the saved route.

There is no central database of pairings and no management lease to renew.
The optional monitor, Thermite, or another manager can be absent. Endpoint
agents and their backend services still need to be running.

<div class="pairing-demo" data-pairing-demo>
  <div class="demo-heading"><span class="section-kicker">Interactive concept</span><h3>Change the route. Close the client.</h3><p>This local demonstration illustrates saved pairing behavior. It is not live device status and sends no network commands.</p></div>
  <div class="demo-routes" aria-label="Demonstration display pairings">
    <div class="demo-route" data-demo-route="display-a" data-demo-initial-source="studio" data-route-state="paired"><span class="demo-destination">Meeting room display</span><strong data-demo-route-name>Studio PC</strong><span data-demo-route-state>Paired · input available</span></div>
    <div class="demo-route" data-demo-route="display-b" data-route-state="unpaired"><span class="demo-destination">Workbench display</span><strong data-demo-route-name>No input selected</strong><span data-demo-route-state>Unpaired</span></div>
  </div>
  <div data-demo-controls hidden>
    <div class="demo-choices"><label>Input<select data-demo-source><option value="studio">Studio PC</option><option value="laptop">Laptop</option></select></label><label>Display<select data-demo-destination><option value="display-a">Meeting room display</option><option value="display-b">Workbench display</option></select></label></div>
    <div class="demo-actions"><button type="button" data-demo-pair>Save pairing</button><button type="button" data-demo-unpair>Unpair display</button></div>
    <div class="demo-environment"><p><span>Control client</span><strong data-demo-controller-state>Open</strong></p><button type="button" data-demo-controller>Close control client</button><p><span>Selected input</span><strong data-demo-source-state>Studio PC: available</strong></p><button type="button" data-demo-source-toggle>Take selected input offline</button></div>
    <p class="demo-status" role="status" aria-live="polite" aria-atomic="true" data-demo-status>Try a different pairing, close the control client, or take an input offline.</p>
  </div>
  <p class="demo-caption">The pairing is desired configuration. An unavailable input leaves it pending; only a new pairing or an explicit unpair changes it. Available input in this illustration does not verify physical video output. Reloading resets the demonstration.</p>
  <noscript><p>The static example shows one paired display and one unpaired display. Saved pairings remain after a control client exits.</p></noscript>
</div>

## 4. Keep the traffic on its own path

For HDMI, the driver may select an appliance channel or start an external
receiver. For USB/IP, the consumer imports devices through the Linux USB
stack. The Yoke agent configures that path and never receives or inspects
the media payload.

The control protocol uses authenticated multicast on the local network.
Payload transport depends on the backend. Protected-content handling
stays with the endpoint’s permitted media path; the agent never decrypts
it or changes protection behavior.

## 5. Observe from anywhere on the control network

```sh
yoke status
yoke snoop
yoke top
```

`status` gives a recent view of endpoint announcements. `snoop` shows the
control conversation. `top` adds an optional terminal dashboard. Observers
send no application control traffic and do not own the routes they show.

The control network uses a shared site key: everyone holding that key
belongs to the same trust domain. The [security guide](../../SECURITY.md)
describes the exact boundary.

<div class="link-cards"><a class="link-card" href="start.md"><strong>Try it without devices</strong><span>Run the pairing fault suite and preview the monitor.</span></a><a class="link-card" href="guides/pairing.md"><strong>Pair configured endpoints</strong><span>Choose a source, inspect acceptance, and verify activation.</span></a><a class="link-card" href="../../doc/design/ARCHITECTURE.md"><strong>Read the architecture</strong><span>Go deeper into persistence, convergence, and drivers.</span></a></div>
