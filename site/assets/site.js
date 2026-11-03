"use strict";

// Keep desktop documentation visible and preserve the mobile disclosure choice.
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

// Resolve the index and result links from this asset, independent of page depth.
const assetURL = new URL(document.currentScript.src);
const indexURL = new URL("../search-index.json", assetURL);
const searchTokens = text => (text.toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu) || [])
  .map(word => {
    if (word.length > 5 && word.endsWith("ing")) return word.slice(0, -3);
    if (word.length > 4 && word.endsWith("ed")) return word.slice(0, -2);
    if (word.length > 3 && word.endsWith("s")) return word.slice(0, -1);
    return word;
  });
// Short terms such as IR must be whole words, rather than matches inside "pair".
const matchesWord = (tokens, word) => tokens.some(token =>
  word.length <= 3 ? token === word : token.startsWith(word)
);
const searchScore = (page, words) => {
  let score = words.reduce((value, word) => value
    + (matchesWord(page.titleTokens, word) ? 10 : 0)
    + (matchesWord(page.summaryTokens, word) ? 5 : 0), 0);
  if (page.titleTokens.join(" ") === words.join(" ")) score += 10;
  // Broad task searches should reach the guide before historical decisions.
  return page.url.startsWith("decisions/") ? score * .6 : score;
};
for (const search of document.querySelectorAll("[data-doc-search]")) {
  const input = search.querySelector("input");
  const results = search.querySelector(".search-results");
  const status = search.querySelector(".search-status");
  if (!input || !results || !status) continue;
  let index = [];
  const render = () => {
    const words = searchTokens(input.value);
    results.replaceChildren();
    if (input.value.trim().length < 2 || !words.length) {
      status.textContent = "Search all documentation. Enter at least two characters.";
      return;
    }
    const matches = index.filter(page => words.every(word =>
      matchesWord(page.tokens, word)
    )).map(page => ({
      page,
      score:searchScore(page, words)
    })).sort((left, right) => right.score - left.score)
      .slice(0, 12).map(match => match.page);
    status.textContent = matches.length
      ? `${matches.length} matching ${matches.length === 1 ? "page" : "pages"}`
      : "No matching pages. Try a broader term.";
    for (const page of matches) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = new URL(page.url, indexURL).href;
      link.textContent = page.title;
      const description = document.createElement("p");
      description.textContent = page.summary;
      item.append(link, description);
      results.append(item);
    }
  };
  fetch(indexURL).then(response => {
    if (!response.ok) throw new Error("Search index unavailable");
    return response.json();
  }).then(pages => {
    index = pages.map(page => ({
      ...page,
      titleTokens:searchTokens(page.title),
      summaryTokens:searchTokens(page.summary),
      tokens:searchTokens(`${page.title} ${page.summary} ${page.text}`)
    }));
    input.disabled = false;
    input.addEventListener("input", render);
    render();
  }).catch(() => {
    status.textContent = "Search is unavailable. Use the documentation links to browse.";
  });
}
