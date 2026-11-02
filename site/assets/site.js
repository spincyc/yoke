"use strict";
// Desktop always exposes the links; mobile remembers the user's disclosure choice.
const navigation = document.querySelector(".navigation");
if (navigation) {
  const mobileViewport = window.matchMedia("(max-width: 760px)");
  let mobileOpen = false;
  const updateNavigation = () => {
    navigation.open = !mobileViewport.matches || mobileOpen;
  };
  navigation.addEventListener("toggle", () => {
    if (mobileViewport.matches) mobileOpen = navigation.open;
  });
  mobileViewport.addEventListener("change", updateNavigation);
  updateNavigation();
}
const search = document.querySelector("[data-doc-search]");
if (search) {
  const input = search.querySelector("input");
  const results = search.querySelector(".search-results");
  const status = search.querySelector(".search-status");
  let index = [];
  const render = () => {
    const query = input.value.trim().toLocaleLowerCase();
    results.replaceChildren();
    if (query.length < 2) {
      status.textContent = "Search titles and page summaries. Enter at least two characters.";
      return;
    }
    const matches = index.filter(page =>
      (page.title + " " + page.summary + " " + page.text)
        .toLocaleLowerCase().includes(query)
    ).slice(0, 12);
    status.textContent = matches.length
      ? `${matches.length} matching pages`
      : "No matching pages. Try a broader term.";
    for (const page of matches) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = page.url;
      link.textContent = page.title;
      const description = document.createElement("p");
      description.textContent = page.summary;
      item.append(link, description);
      results.append(item);
    }
  };
  fetch("search-index.json").then(response => {
    if (!response.ok) throw new Error("Search index unavailable");
    return response.json();
  }).then(pages => {
    index = pages;
    input.disabled = false;
    input.addEventListener("input", render);
    render();
  }).catch(() => {
    status.textContent = "Search is unavailable here. Browse the navigation, or serve this site over HTTP.";
  });
}
