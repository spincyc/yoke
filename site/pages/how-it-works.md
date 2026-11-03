<header class="product-intro"><p class="eyebrow">How it works</p><h1>Connect the boxes, then pair them.</h1>
<p class="hero-lead">The USB device connects to one box. Your computer connects by USB to a second box. Ethernet carries the connection between them.</p></header>

<figure class="product-figure"><picture><source media="(max-width: 760px)" srcset="../assets/diagrams/control-and-payload-mobile.svg"><img src="../assets/diagrams/control-and-payload.svg" alt="Two Yoke USB hardware boxes connect a USB device to a separate computer through Ethernet. An optional setup tool pairs the boxes and can then close."></picture><figcaption>The setup tool can close after pairing. Both hardware boxes keep running.</figcaption></figure>

<span id="1-give-each-end-an-identity" class="legacy-anchor" aria-hidden="true"></span>
## 1. Connect the USB cables

Plug the USB device into the device-side box. Connect the computer-side
box to your computer with another USB cable. Both Yoke boxes are separate from your computer.

<span id="2-choose-the-source-for-a-destination" class="legacy-anchor" aria-hidden="true"></span>
## 2. Connect the boxes to Ethernet

Both boxes connect to your Ethernet switch. The prototype uses a Radxa
ZERO 3E board in each box and targets USB 2 first.

<span id="3-let-the-endpoints-keep-the-route" class="legacy-anchor" aria-hidden="true"></span>
## 3. Pair the boxes

Tell the computer-side box which device-side box to use. A command looks
like this, using the names assigned to your boxes:

```sh
yoke pair usb-device-box desk-usb-box
```

The box saves that choice. Closing the setup tool does not clear it.
After a restart or a lost network connection, it keeps the pairing and
tries to reconnect. Use a new pair or unpair command to change it.

<div class="pairing-demo" data-pairing-demo>
<div class="demo-heading"><h3>Try pairing a box</h3><p>This example runs in your browser. It does not connect to any hardware.</p></div>
<div class="demo-routes" aria-label="Example USB pairing">
<div class="demo-route" data-demo-route="display-a" data-demo-initial-source="controller" data-route-state="paired"><span class="demo-destination">Box beside your computer</span><strong data-demo-route-name>USB controller box</strong><span data-demo-route-state>Paired · device box online</span></div>
</div>
<div data-demo-controls hidden>
<div class="demo-choices"><label>Device-side box<select data-demo-source><option value="controller">USB controller box</option><option value="keyboard">Keyboard box</option></select></label><label>Computer-side box<select data-demo-destination><option value="display-a">Box beside your computer</option></select></label></div>
<div class="demo-actions"><button type="button" data-demo-pair>Pair boxes</button><button type="button" data-demo-unpair>Unpair</button></div>
<div class="demo-environment"><p><span>Setup tool</span><strong data-demo-controller-state>Open</strong></p><button type="button" data-demo-controller>Close setup tool</button><p><span>Device-side box</span><strong data-demo-source-state>USB controller box: online</strong></p><button type="button" data-demo-source-toggle>Disconnect device box</button></div>
<p class="demo-status" role="status" aria-live="polite" aria-atomic="true" data-demo-status>Choose a device box, close the setup tool, or disconnect a box.</p>
</div>
<p class="demo-caption">Disconnecting a box keeps its pairing saved. Pair or unpair changes it. Reloading this page resets the example.</p>
<noscript><p>The example shows the computer-side box paired with the USB controller box. That pairing stays saved after the setup tool closes.</p></noscript>
</div>

<span id="4-keep-the-traffic-on-its-own-path" class="legacy-anchor" aria-hidden="true"></span>
## 4. Use the device from your computer

The computer sends and receives USB traffic through its USB cable. The two
boxes carry that traffic over Ethernet. The boxes handle the connection; your computer uses its USB port.

This is the agreed hardware design. The complete USB bridge firmware and
hardware tests are still unfinished. See the
[USB hardware page](usb.md) and [development status](docs/project-status.md).

<span id="5-observe-from-anywhere-on-the-control-network" class="legacy-anchor" aria-hidden="true"></span>
## Optional management and monitoring

Use the command line, Thermite, or another manager to change pairings.
The boxes do not depend on a manager remaining connected.

```sh
yoke status
yoke snoop
yoke top
```

These tools show saved pairings, recent status, and errors. A status
message cannot establish that a physical device is working; hardware
tests check that separately.

[USB hardware](usb.md) · [Software tests](start.md) ·
[Technical documentation](engineering/index.md)
