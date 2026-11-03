# Yoke identity and interface guide

Yoke pairs endpoints while keeping control at the edge. Its identity should
feel like the equipment and tools an operator can depend on: clear, quiet,
and easy to identify. Use the same name and mark on documentation,
interfaces, and proposed endpoint enclosures.

![Yoke wordmark](../../site/assets/yoke-lockup.svg)

## Name and voice

Write **Yoke** in prose. Use `yoke` for commands, package names, and code;
use **Yoke** in the outlined website wordmark. Existing **YOKE** equipment
labels remain valid. The short line
is **Control at the edge.** The explanatory description is **Controllerless
pairing for HDMI and USB endpoints over switched Ethernet.**

Lead with an operator's task, then state the evidence and any limit. Keep
commands, endpoint IDs, error messages, and measured units exact. Distinguish
configured pairing, backend readiness, and independently observed media.
Describe proposed hardware as proposed until a documented bench result
supports a stronger claim. Avoid decorative slogans in operational output.

## Mark and source assets

The mark is a symmetric yoke: two equal uprights join one lower stem. Its
shape references paired endpoints and a shared link. It is drawn as one
solid silhouette so the same geometry works on screen and on a part.

| Asset | Use |
|---|---|
| [yoke-mark.svg](../../site/assets/yoke-mark.svg) | Canonical monochrome geometry; CAD import, engraving, and single-color reproduction |
| [yoke-lockup.svg](../../site/assets/yoke-lockup.svg) | Mark and outlined Instrument Sans wordmark; site header, README, and document covers |
| [favicon.svg](../../site/assets/favicon.svg) | Small browser icon, with a quiet neutral background |
| [site.css](../../site/assets/site.css) | Authoritative interface color, type, spacing, and responsive tokens |

`yoke-mark.svg` is the only geometry authority for the symbol. It has a
`0 0 32 32` viewBox and one closed, black filled path. It contains no font,
stroke, transformation, embedded image, or external reference. The visual
bounds are **x = 4…28, y = 3…29**: 24 units wide and 26 units tall. Arms,
curved band, and stem are 6 units thick. The inner and outer arcs have
radii of 6 and 12 units. Do not replace it with a typed character.

The lockup and favicon repeat the canonical path as presentation
derivatives. Their first symbol path must match the canonical path exactly;
the asset test checks this. The title-case wordmark is outlined Instrument Sans at weight 620. Its
geometry is a web presentation asset; it is independent of the canonical
manufacturing mark and does not require an installed font. Both the site and the lockup support the
system's light or dark preference without downloading assets.

Use the mark in one solid foreground color. Preserve its proportions and
open upper center. Do not outline, stretch, tilt, animate, add a gradient,
or enclose it in a new badge. Use the supplied favicon for browser contexts.
Leave at least one band width (6 source units) clear around the visible
mark in normal layouts. The SVG canvas alone supplies only part of that
clear space. In a compact application icon, the supplied canvas is the
minimum padding. Use the lockup at 104 CSS pixels wide or larger and the
standalone mark at 24 CSS pixels wide or larger. The favicon is the small
size exception.

## Color

The palette pairs warm offwhite with charcoal and one restrained cobalt
accent. Product pages use space, type, and device illustrations to explain
Yoke. Documentation uses the same identity at a quieter reading scale.
Cobalt identifies links, primary actions, and selected states; it is not an
operational success indicator. Status always needs words as well as color.

| Role / CSS token | Light | Dark |
|---|---|---|
| Heading and identity / `--ink` | `#17202A` | `#F3F4F6` |
| Body text / `--text` | `#39424D` | `#C6CBD2` |
| Secondary text / `--muted` | `#626B76` | `#9AA4B2` |
| Page / `--paper` | `#F8F7F4` | `#15191F` |
| Elevated surface / `--surface` | `#FFFFFF` | `#1B2028` |
| Quiet panel / `--panel` | `#EEEDE9` | `#202630` |
| Decorative rule / `--line` | `#D9DCE0` | `#353D49` |
| Selection surface / `--accent` | `#E8EEFC` | `#223354` |
| Text on selection / `--accent-ink` | `#244D9D` | `#C6D6FF` |
| Link / `--link` | `#2452BC` | `#A2BDFF` |
| Identity accent and focus / `--cobalt` | `#315FCD` | `#82A7FF` |
| Inline code / `--code` | `#EEEFEF` | `#262E39` |
| Research notice / `--warning` | `#F3EEE0` | `#302E27` |
| Code block / `--code-bg` | `#202732` | `#10141B` |
| Code text / `--code-text` | `#ECF0F7` | `#E5EBF7` |
| Primary action / `--button-bg` | `#2452BC` | `#A2BDFF` |
| Action text / `--button-text` | `#FFFFFF` | `#142342` |

Normal text needs at least 4.5:1 contrast against its actual surface. Focus
indicators and interactive boundaries need at least 3:1. `--line` supplies
decorative separators; input boundaries use `--muted`. Prose links remain
underlined. Keyboard focus uses a visible 3-pixel outline with space around
it. Navigation identifies the current page through `aria-current`, weight,
and a non-color cue.

## Typography and layout

The website serves **Instrument Sans** locally from
[`site/assets/fonts`](../../site/assets/fonts). Its full variable upright
and italic fonts preserve the upstream glyph set and support weight 400–700. They are
redistributed under the [SIL Open Font License 1.1](../../site/assets/fonts/OFL.txt).
The font source and conversion are recorded beside the asset. Arial and
Helvetica provide fallbacks. Commands and identifiers use the local
monospace stack. No external runtime resources or font services are used.

Product headings use weight 500, tight tracking, and generous whitespace.
The large opening statement belongs to the product page; documentation
headings stay smaller. Body copy uses regular weight and a 1.65 line height.
Keep a readable documentation measure, short paragraphs, and technical
tables that scroll in their own boxes. Do not shrink the page to fit code.

The product layout has no documentation sidebar. Its opening pairs a plain
product statement with an illustration of familiar devices and connections.
Describe hardware illustrations as concepts. Follow with broad editorial
sections, clear connection paths, and concrete uses. Avoid decorative
terminal windows, dashboard furniture, and repeated stacks of small cards.

Documentation has curated navigation, a hub with local search, breadcrumbs,
on-page headings, and previous/next links. The 760-pixel navigation
breakpoint is shared with `site.js`. Preserve keyboard access after viewport
changes and when JavaScript is unavailable. Use 4-pixel spacing steps,
1-pixel rules, restrained 4–7-pixel corner radii, and no decorative shadows.

Diagrams use the surrounding palette and clear labels. Give every image a
useful alternative description and every inline diagram an accessible title
and description. Distinguish endpoint media from control traffic. An
interactive concept must identify its simulated behavior and retain a useful
static explanation without JavaScript. Respect reduced-motion preferences.
Do not give the identity mark an operational status meaning.

## Equipment and CAD reproduction

Import the canonical [yoke-mark.svg](../../site/assets/yoke-mark.svg), rather
than a lockup or screenshot. Its black area is the intended solid or
recessed region. SVG coordinates run down the page; CAD may need a Y-axis
flip to keep the upright mark facing outward. Verify the assembled exterior
view, scale, and orientation before export.

For the proposed USB housing R2 lid, use a monochrome recessed mark with
**8 mm visible width**, **8.667 mm visible height**, and **0.45 mm depth**.
The SVG scale is 8/24 = 1/3 mm per source unit, so the band is 2 mm wide.
Place it on a clear exterior region away from vents, wall edges, fasteners,
connector openings, and required engineering labels. The housing source
owns the exact position and records how it imports the mark.

Use at least 1.2 mm positive features and negative clearances in future
FDM applications, and prefer the 8 mm mark or larger. These are conservative
design dimensions, not a claim of a validated printer/material finish.
Review the sliced first layers and recess before printing. Use one material
color; no color-change or extra print job is required for recognition.
Keep the mark independent of revision IDs, port labels, electrical ratings,
and safety markings. An engraving does not establish a part's mechanical
or electrical fitness.

The R1 enclosure retains its existing YOKE lettering and released
geometry. Branding revisions belong in a new CAD revision; never revise a
released artifact or a running print to apply an identity update. A future
revision that changes the mark must deliberately update its source
reference and reproduce the derivative asset check.

## Terminal and code use

Keep command output plain, compact, and useful when copied into an issue.
Do not add a logo banner, forced ANSI color, Unicode art, or animation to a
CLI. In human documentation, use `yoke pair …`, `yoke status`, and
`yoke snoop` exactly as commands. Keep endpoint IDs in monospace, and show
status as a word: `active`, `pending`, or `error`, with the backend detail
when it matters.

Brand colors may decorate a terminal or editor theme, but output must remain
understandable in monochrome and when redirected. Naming and appearance do
not alter YWP/1 frames, authentication, wire values, configuration keys,
exit codes, or protocol behavior. Protocol changes remain governed by the
[normative wire specification](PROTOCOL.md).
