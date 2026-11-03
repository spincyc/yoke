/* A local illustration of pairing semantics. It performs no device or network I/O. */
(() => {
  "use strict";

  document.querySelectorAll("[data-pairing-demo]").forEach((demo) => {
    const source = demo.querySelector("[data-demo-source]");
    const destination = demo.querySelector("[data-demo-destination]");
    const pair = demo.querySelector("[data-demo-pair]");
    const unpair = demo.querySelector("[data-demo-unpair]");
    const controller = demo.querySelector("[data-demo-controller]");
    const sourceToggle = demo.querySelector("[data-demo-source-toggle]");
    const controllerState = demo.querySelector("[data-demo-controller-state]");
    const sourceState = demo.querySelector("[data-demo-source-state]");
    const status = demo.querySelector("[data-demo-status]");
    const controls = demo.querySelector("[data-demo-controls]");

    if (!source || !destination || !pair || !unpair || !controller ||
        !sourceToggle || !controllerState || !sourceState || !status || !controls) {
      return;
    }

    const sourceNames = new Map(
      Array.from(source.options, (option) => [option.value, option.textContent.trim()]),
    );
    const destinationNames = new Map(
      Array.from(destination.options, (option) => [option.value, option.textContent.trim()]),
    );
    const online = new Map(Array.from(sourceNames.keys(), (id) => [id, true]));
    const routes = new Map();
    const routeElements = Array.from(demo.querySelectorAll("[data-demo-route]"));
    let clientOpen = true;

    routeElements.forEach((element) => {
      const initial = element.dataset.demoInitialSource || null;
      routes.set(element.dataset.demoRoute, sourceNames.has(initial) ? initial : null);
    });

    function render() {
      const sourceId = source.value;
      const sourceName = sourceNames.get(sourceId);
      const sourceOnline = online.get(sourceId);
      controllerState.textContent = clientOpen ? "Open" : "Closed; pairing stays saved";
      controller.textContent = clientOpen ? "Close setup tool" : "Open setup tool";
      sourceState.textContent = `${sourceName}: ${sourceOnline ? "online" : "offline"}`;
      sourceToggle.textContent = sourceOnline ? "Disconnect device box" : "Reconnect device box";
      pair.disabled = !clientOpen;
      unpair.disabled = !clientOpen || !routes.get(destination.value);

      routeElements.forEach((element) => {
        const id = element.dataset.demoRoute;
        const configuredSource = routes.get(id);
        const name = element.querySelector("[data-demo-route-name]");
        const state = element.querySelector("[data-demo-route-state]");
        if (!name || !state) return;
        name.textContent = configuredSource ? sourceNames.get(configuredSource) : "Not paired";
        if (!configuredSource) {
          element.dataset.routeState = "unpaired";
          state.textContent = "Unpaired";
        } else if (!online.get(configuredSource)) {
          element.dataset.routeState = "pending";
          state.textContent = "Waiting for device box; pairing saved";
        } else {
          element.dataset.routeState = "paired";
          state.textContent = "Paired · device box online";
        }
      });
    }

    function announce(message) {
      render();
      status.textContent = message;
    }

    pair.addEventListener("click", () => {
      if (!clientOpen || !sourceNames.has(source.value) || !routes.has(destination.value)) return;
      routes.set(destination.value, source.value);
      const availability = online.get(source.value)
        ? "The device-side box is online in this example."
        : "The device-side box is offline; the pairing stays saved.";
      announce(`${destinationNames.get(destination.value)} is now paired with ${sourceNames.get(source.value)}. ${availability}`);
    });

    unpair.addEventListener("click", () => {
      if (!clientOpen || !routes.has(destination.value)) return;
      routes.set(destination.value, null);
      announce(`${destinationNames.get(destination.value)} is now unpaired.`);
    });

    controller.addEventListener("click", () => {
      clientOpen = !clientOpen;
      announce(clientOpen
        ? "Setup tool opened. You can change the pairing."
        : "Setup tool closed. The computer-side box keeps its saved pairing.");
    });

    sourceToggle.addEventListener("click", () => {
      const id = source.value;
      if (!online.has(id)) return;
      online.set(id, !online.get(id));
      announce(online.get(id)
        ? `${sourceNames.get(id)} is online again. The paired computer-side box can reconnect.`
        : `${sourceNames.get(id)} is offline. The computer-side box keeps its pairing while the device box is offline.`);
    });

    source.addEventListener("change", render);
    destination.addEventListener("change", render);
    render();
    controls.hidden = false;
    demo.dataset.demoReady = "true";
  });
})();
