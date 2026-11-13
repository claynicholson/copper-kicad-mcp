# Focus — point at the design, and any AI knows what you mean

"Move **this** cap closer to U2." "Why are **these** two nets swapped?" "Put the connector **here**."

Focus is how the human's *this / these / here* reaches the model. It works with every MCP client and every
model: the server never calls an AI, it only hands whichever model is driving a plain JSON object and a picture.

There are three ways to point. All of them end in the same **focus object**:

```json
{"document": "pcb", "mode": "box", "box_mm": [117.5, 111.1, 121.1, 114.0],
 "refs": ["R4", "R5"], "nets": ["+3V3", "/BOOT", "/LED_K", "/LED"], "age_s": 4.2, "source": "canvas"}
```

| field | meaning |
|---|---|
| `document` | `schematic` or `pcb` |
| `sheet`, `sheet_file` | schematic only: sheet path (`/`, `/amp/left`) and its file. One file used twice = two sheets with different references |
| `mode` | `point` (a click), `box` (a drag), `selection` (shift-added parts, or a KiCad selection) |
| `point_mm` / `box_mm` | where, in KiCad millimetres of that document (`[x, y]` / `[x0, y0, x1, y1]`) |
| `refs` | the part under the cursor, or every part inside **or touching** the box (natural order) |
| `pins` | `REF.PIN` — the pin/pad under the cursor; for a box, the pins it cut out of a part it only partly covers |
| `nets` | nets of those pins (a click on a part body or a box: every net involved; a click on a track: that net) |
| `age_s` | seconds since the user pointed |
| `source` | `canvas`, `kicad` or `args` |
| also | `nearest` (click on empty space: closest part and distance), `parts_within_mm` (the click was outside every part: the box that holds them), `also` (other parts under the same spot — the other board side or an overlapping courtyard, e.g. `["C37 (bottom)"]`), `sheets` (a sheet symbol was hit), `tracks` (segments in the box), `pins_total` (more pins than listed) |

Empty `refs` with a `point_mm` / `box_mm` is meaningful: the user means that **area**.

---

## For the user: how to point

### A. Copper Canvas (works everywhere, schematic and board)

Ask your AI to "open the canvas" (it calls `focus(action='open')`), or run `copper canvas` yourself
(`python -m copper_kicad.focus [project]` does the same). A browser tab opens on `http://127.0.0.1:<port>/?t=<token>`
showing your schematic sheets and the board, drawn by KiCad's own SVG export.

| do this | and the focus becomes |
|---|---|
| hover | nothing yet — the nearest part lights up with its reference, value and nets |
| **click** a part | that part (`R3`) |
| **click** a pin or pad | that pin (`U2.EN`) and its net. On a two-sided board the side you are looking at wins; a pad under the cursor wins over a courtyard that merely covers the spot |
| **drag** | a box: everything inside or touching it. On the board: parts on the side you are looking at — parts on the far side join in only when that side's silkscreen or fab layer is switched on |
| **shift**-click / shift-drag | add to the focus (shift-click a selected part again to drop it) |
| click empty space | that spot ("put it here") |
| click a track (board) | that net |
| **Esc** or the × on the chip | clear |

The chip in the top-right corner reads the focus back to you — `R3, C5, U2.EN` is exactly what the AI will get
(click it to copy). Then just talk: *"move these next to the regulator"*.

Getting around: wheel = zoom, right- or middle-drag (or hold space) = pan, `F` = fit. Tabs switch between
schematic sheets, PCB top and PCB bottom (mirrored, as if you flipped the board); the layer chips show or hide
layers. Double-click a hierarchical sheet symbol to open that sheet.

The canvas **reloads by itself** whenever a design file changes, so you can watch the AI's edits land.

### B. Select in KiCad's PCB editor (optional)

If KiCad's API is enabled (Preferences ▸ Plugins ▸ *Enable KiCad API*, or `copper kicad-api enable`) and the
optional `kicad-python` package is installed in Copper's Python, whatever you have selected in the PCB editor —
a box-drag there *is* the bounding box — is the focus. Parts, pads, tracks, vias and zones all count.

Stock KiCad 9/10 have **no schematic API**, so a schematic selection cannot be read there; use the canvas for
schematics.

### B2. Inside the Copper app

The Copper app (the KiCad fork) needs neither the canvas nor the API: it builds the focus object itself from
the live selection, cursor point or dragged box in the schematic and the board editor, and hands it to the
agent with every message (`focus.changed` and the `focus` of `turn.start`, see
[HOST_PROTOCOL.md](internal/HOST_PROTOCOL.md) section 4.7). That object has `source: "kicad"` and adds `sheet_path`,
`items` (uuids), `seq` and `rev` to the fields below. The `focus` tool is not offered to the model there.

### C. Just say it

Coordinates and references in chat work too — the AI resolves them with `focus(action='resolve', …)`.

### Privacy and safety

The canvas listens on `127.0.0.1` only, on a random port, and every request needs the random token in the URL
(the Host header must be loopback too). It serves three built-in assets and your project's KiCad exports —
no file paths, no shell, no network requests, no CDN. Focus state and SVG caches live under `~/.copper/`
(`COPPER_HOME`), never in your project folder. The canvas never changes the design.

---

## For the model: how to use it

One tool, `focus`:

| call | use it when |
|---|---|
| `focus()` | the user said "this / these / here / that corner". Returns the latest focus, the parts in it and a cropped picture |
| `focus(wait=60)` | you just asked them to point: waits for their **next** click or box (returns the old focus with `timed_out` if none comes) |
| `focus(action='open')` | nothing pointed yet (`focus: null`) and no canvas running. Opens the browser; if `launched` is false give the user `url` |
| `focus(action='resolve', document='pcb', box=[x0,y0,x1,y1])` | you have coordinates (a DRC finding's `at`, a region you chose) and want to know what is there. `near=[x,y]` (+`radius`) for a point; `sheet='/amp'` for a schematic sheet; `side='bottom'` to restrict |
| `focus(action='clear')` | the reference is used up and should not be picked up again |
| `focus(source='kicad')` | you specifically want the KiCad PCB selection (errors with the exact fix if the API is off) |

Result:

```json
{"focus": {…the object above…}, "chip": "R4, R5",
 "items": [{"ref": "R4", "value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric",
            "at": [118.5, 112.5], "rot": -90, "side": "top", "nets": ["+3V3", "/BOOT"]}, …],
 "stale": true, "missing": ["R9"]}
```

plus one PNG: the focused region rendered by KiCad, the user's box / point drawn in, each referenced part outlined
and labelled. Look at it — it is what the user sees.

Rules of thumb:

* `items` are **re-read from the files on every call**, so positions, values and nets are current even if the
  focus is old. `stale: true` says the design changed since the user pointed; `missing` lists references that no
  longer exist. With a large `age_s` or `stale`, confirm before a destructive edit.
* `also` on a point focus lists other parts under the same spot (two-sided boards). If the user's words fit one of
  those better, re-resolve with `side='bottom'` / `side='top'` rather than guessing.
* A box over a large area lists every reference in `refs`, but details at most 40 `items` (`items_total`,
  `items_note`) and 12 pins per item / 24 in `focus.pins` (`pins_total`). Use a smaller box for the rest.
* `inside: "partial"` on an item means the box only clipped it (often a big module next to the parts the user
  really meant). `pins` on such an item are the pins that were inside the box.
* Feed the result straight into the other tools: `refs` / `pins` → `sch_apply` ops (`move`, `connect`, `set_field`…),
  `pcb_place(moves=[…])`, `pcb_route`; `box_mm` → `render(crop=…)`; `point_mm` → an `at` for a new part.
* A schematic focus and a board focus name the **same references** — a part pointed at on the schematic can be
  moved on the board and vice versa.
* Never ask the user for coordinates. Ask them to point, then `focus(wait=60)`.

### For tool authors (Python)

```python
from copper_kicad import focus

focus.resolve(pr, document, box=None, near=None, radius=None, sheet=None, side=None, prefer_side=None) -> dict
focus.select(pr, document, refs, pins=(), sheet=None) -> dict      # same shape, from explicit references
focus.latest(pr=None) -> dict | None                               # newest stored focus (+age_s); file read only
focus.as_resource(pr=None) -> {"uri": "copper://focus", "mimeType": "application/json", "text": "…"}
focus.current(pr, source="auto", wait=0) -> (focus | None, info)
focus.detail(pr, focus_obj, render=True) -> (payload, [Image])
focus.canvas.start(pr, launch=False) -> {"url", "port", "pid", "reused", "launched"}
focus.canvas.serve(project=None, port=0, launch=True) -> int       # blocking; the `copper canvas` command
```

`resolve` works from the files alone (no canvas, no KiCad GUI) and returns
`{document, sheet?, sheet_file?, mode, box_mm|point_mm, radius_mm?, refs, pins?, nets, items, nearest?, parts_within_mm?, also?, tracks?, sheets?, stamp}`
where each item is `{ref, value, footprint, x, y, rot, bbox, nets, inside?, pins?, side|unit}`.
It is what a `region=[x0,y0,x1,y1]` / `near=[x,y]` parameter on any other tool should call.

### How the coordinates stay exact

KiCad's SVG export uses millimetres as SVG user units: a schematic sheet plots with `viewBox="0 0 <page mm>"`,
and a board plotted with `--page-size-mode 1` keeps true board coordinates. The canvas only rewrites the root
`viewBox` to choose the visible region (for sheets: to exactly the page size — kicad-cli itself writes 297.0022 for
a 297 mm page, which would otherwise stretch the drawing by a few µm), and maps `screen = origin + (mm − view) × scale` (X mirrored for the
bottom view). `tests/test_focus.py` proves the chain: every pin tip and pad centre from the design files is found
at the identical number in KiCad's SVG, and round-trips through the same transform the page runs (the test
executes `canvas.js` itself when `node` is installed).

### Limits

* The KiCad selection channel is PCB-only (no schematic IPC in KiCad 9/10) and reports values / nets from the
  **saved** board file.
* Hit boxes are courtyards (board) and symbol body + pins (schematic), as axis-aligned boxes; a part with no
  courtyard uses its pads and graphics. Pads are tested exactly, in their own rotated frame (rounded corners count
  as the rectangle; a custom-shaped pad as the box around its copper). Where a top and a bottom pad sit on the
  very same spot and no side is given, the smaller part wins and the other is named in `also`. Wires, text, zones and graphics are not pointable on the canvas (a box still reports the nets of
  labels and tracks inside it).
* A focus lives on one document and one sheet; shift-adding across sheets starts a new focus.
* One canvas per project. `focus(action='open')` re-uses a canvas that is already running, including one started
  with `copper canvas`; a canvas started by the tool lives as long as the MCP server process.
* Very large boards take a while on first load (an 85 MB, 10-layer board: about 12 s to index and 8 s for KiCad
  to plot); after that everything is cached until the file changes.
