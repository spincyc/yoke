# Yoke identity and interface guide

Yoke pairs endpoints while keeping control at the edge. Its identity should
feel like the equipment and tools an operator can depend on: clear, quiet,
and easy to identify. Use the same name and mark on documentation,
interfaces, and proposed endpoint enclosures.

![Yoke wordmark](../../site/assets/yoke-lockup.svg)

## Name and voice

Write **Yoke** in prose. Use `yoke` for commands, package names, and code;
use **YOKE** in the drawn wordmark and on equipment labels. The short line
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
| [yoke-lockup.svg](../../site/assets/yoke-lockup.svg) | Mark and custom outlined YOKE wordmark; site header, README, and document covers |
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
the asset test checks this. The wordmark is outlined geometry, so it does
not depend on an installed font. Both the site and the lockup support the
system's light or dark preference without downloading assets.

Use the mark in one solid foreground color. Preserve its proportions and
open upper center. Do not outline, stretch, tilt, animate, add a gradient,
or enclose it in a new badge. Use the supplied favicon for browser contexts.
Leave at least one band width (6 source units) clear around the visible
mark in normal layouts. The SVG canvas alone supplies only part of that
clear space. In a compact application icon, the supplied canvas is the
minimum padding. Use the lockup at 110 CSS pixels wide or larger and the
standalone mark at 24 CSS pixels wide or larger. The favicon is the small
size exception.

## Color

Evergreen identifies actions and links. Pale sage is a selected surface,
not a warning or an indicator that a device is operational. Most of the
interface is neutral space, text, and fine rules. Use color sparingly;
status always needs a word as well as a color.

| Role / CSS token | Light | Dark |
|---|---|---|
| Heading and primary identity / `--ink` | `#203B32` | `#E2ECE5` |
| Body text / `--text` | `#364B42` | `#CBD8CF` |
| Secondary text / `--muted` | `#5C7065` | `#A3B6A9` |
| Page / `--paper` | `#FAFBF8` | `#151E19` |
| Quiet panel / `--panel` | `#EEF2ED` | `#1D2A22` |
| Decorative rule / `--line` | `#D7DFD7` | `#36483B` |
| Selection surface / `--accent` | `#DCE8DF` | `#2D4C3B` |
| Text on selection / `--accent-ink` | `#244D3E` | `#D4E9DA` |
| Link and focus / `--link` | `#275F50` | `#A8D2B8` |
| Inline code / `--code` | `#EDF1EB` | `#27372B` |
| Research notice / `--warning` | `#F3EDDF` | `#302E23` |
| Code block / `--code-bg` | `#182B24` | `#101A13` |
| Code text / `--code-text` | `#E1EAE3` | `#DCE9DF` |
| Primary action / `--button-bg` | `#275F50` | `#A8D2B8` |
| Action text / `--button-text` | `#FFFFFF` | `#18382B` |

Normal text needs at least 4.5:1 contrast with its actual surface; focus
indicators and interactive boundaries need at least 3:1. The subtle
`--line` token is for decorative separators only. Inputs use the stronger
secondary-text color for their border. Links in prose remain underlined.
Focus has a visible 3-pixel outline with space around it. The current-page
navigation also uses stronger weight and `aria-current`.

## Typography and layout

Use the local system sans-serif stack in `site.css`; never load a web font.
Use the local monospace stack for commands, values, source, and identifiers.
The outlined wordmark is an identity asset, not a body font.

Body copy is 1 rem with a 1.7 line height. Headings use moderate weight and
slight negative tracking; large homepage type is reserved for the single
opening statement. Documentation headings stay smaller. Keep paragraphs
short and technical tables readable; allow wide code and tables to scroll
inside their own boxes. Do not shrink the whole page to fit them.

Use a 4-pixel spacing unit, with 8, 12, 16, 24, 32, and 48 pixels as the
normal steps. Prefer alignment and space to nested cards or shadows. Use
1-pixel rules and 3–5-pixel corner radii. The documentation shell keeps its
navigation and reading column aligned; its 760-pixel mobile breakpoint
matches the navigation script. Preserve keyboard access after viewport
changes and when JavaScript is unavailable.

Diagrams use the same surface and text tokens as the surrounding page.
Keep labels as real text, supply an accessible title and description, and
show control paths separately from media paths. The landing diagram has a
text layout on narrow screens. Do not use the brand mark as a status light
or a diagram node with an operational meaning.

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
