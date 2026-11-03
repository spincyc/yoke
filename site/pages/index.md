<span id="connect-endpointskeep-control-at-the-edge" class="legacy-anchor" aria-hidden="true"></span>
<section class="product-hero">
<div class="hero-copy">
<p class="eyebrow">Open connections. Independent endpoints.</p>
<h1>Put your devices<br>where you need them.</h1>
<p class="hero-lead">Yoke is an open hardware and software project for connecting HDMI sources, USB peripherals, and future IR endpoints over Ethernet. Choose a source. Pair a destination. Let the endpoints remember the connection.</p>
<div class="hero-actions"><a class="button" href="start.md">Try the software <span aria-hidden="true">→</span></a><a class="text-link" href="how-it-works.md">How it works <span aria-hidden="true">↗</span></a></div>
<p class="hero-note">In development. Working control software; endpoint hardware is being designed. <a href="docs/project-status.md">See what’s ready.</a></p>
</div>
<div class="hero-visual">
<figure id="network-title" class="product-figure"><picture><source media="(max-width: 760px)" srcset="../assets/diagrams/product-scene-mobile.svg"><img src="../assets/diagrams/product-scene.svg" alt="A computer connects by HDMI to a Yoke input endpoint. Ethernet carries the connection through a switch to a Yoke output endpoint and a display." width="1000" height="680"></picture><figcaption id="network-description">HDMI endpoint concept. Complete endpoint hardware has not been bench-tested.</figcaption></figure>
</div>
</section>

<span id="a-small-command-surface" class="legacy-anchor" aria-hidden="true"></span>
<section class="product-section">
<div class="section-heading"><p class="eyebrow">One network. Useful connections.</p><h2>A connection should follow the task.</h2><p>A computer in the equipment closet. A display at the desk. A USB controller where you actually use it. Yoke is being built to make those connections explicit, changeable, and yours to inspect.</p></div>
<div class="scenario-grid">
<article class="product-scenario"><span class="feature-number">01 / HDMI</span><h3>Choose what’s on the screen.</h3><p>Pair a source with a display endpoint. Change the source from the command line; a local driver configures the video backend.</p><a href="product.md#hdmi">Explore the video path <span aria-hidden="true">→</span></a></article>
<article class="product-scenario"><span class="feature-number">02 / USB</span><h3>Bring a peripheral to a host.</h3><p>The Linux USB/IP reference connects a remote USB device to a Linux computer’s USB stack. The kernel carries the device traffic.</p><a href="product.md#usb">Explore the USB path <span aria-hidden="true">→</span></a></article>
<article class="product-scenario"><span class="feature-number">03 / IR · Planned</span><h3>Reach the equipment in the cabinet.</h3><p>A future IR endpoint would put remote-control signals beside the device they operate. IR routing is a planned part of the project.</p><a href="product.md#ir">Explore the IR direction <span aria-hidden="true">→</span></a></article>
</div>
</section>

<section class="product-section">
<div class="section-heading"><p class="eyebrow">Control belongs with the connection</p><h2>Pair it. Close the app. Keep going.</h2><p>The command-line tool sends a request and exits. Each endpoint saves its own pairing and reconnects from that state. No central controller, cloud account, or management dashboard needs to stay open.</p></div>
<div class="feature-grid">
<div class="feature"><span class="feature-number">Persistent</span><h3>The endpoint remembers.</h3><p>Restart an agent and it restores its saved route. A missing source leaves the pairing in place while the endpoint waits for its return.</p></div>
<div class="feature"><span class="feature-number">Observable</span><h3>See the connection’s state.</h3><p>Inspect who is paired, who is online, and what a driver reports. Independent observers can listen without controlling the system.</p></div>
<div class="feature"><span class="feature-number">Independent</span><h3>Choose your management tools.</h3><p>Use the CLI on its own. Add a terminal dashboard, Prometheus, Thermite, or another manager when you want a wider view.</p></div>
</div>
</section>

<span id="working-software-an-open-hardware-roadmap" class="legacy-anchor" aria-hidden="true"></span>
<span id="follow-the-evidence" class="legacy-anchor" aria-hidden="true"></span>
<section class="product-section status-snapshot">
<div><p class="eyebrow">Start without hardware</p><h2>Try the part you can run today.</h2><p>Run the real pairing state machines through repeatable network faults. Preview the monitor. Explore the software media pipeline. All on one computer.</p></div>
<div class="hero-actions"><a class="button" href="start.md">Get started <span aria-hidden="true">→</span></a><a id="doc-search" class="text-link" href="docs/index.md#search">Search the documentation <span aria-hidden="true">↗</span></a></div>
</section>
