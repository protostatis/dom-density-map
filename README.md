# dom-density-map

Text-based DOM density maps for LLM browser automation — **79% fewer tokens than screenshots**.

Connects to any Chrome instance via CDP (Chrome DevTools Protocol), walks the visible DOM, and renders a character grid showing element density and type. Interactive elements are indexed with labels and pixel coordinates. Designed for LLM agents that need to understand page layout without burning tokens on base64 screenshots.

## Quick Start

```bash
# Run without installing
uvx dom-density-map https://example.com

# Or install
pip install dom-density-map
dom-density-map https://example.com
```

**Prerequisite:** Chrome must be running with remote debugging enabled:

```bash
google-chrome --remote-debugging-port=9222
# or on macOS:
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222
```

## Usage

```bash
# Map the current page
dom-density-map

# Navigate to a URL and map it
dom-density-map https://example.com

# Sparse mode — RLE-compressed, ~420 tokens for a complex page
dom-density-map --sparse --cols 60

# Unicode block art mode
dom-density-map --blocks --cols 80

# Reverse lookup — full DOM stack at a pixel coordinate
dom-density-map --at 694,584

# Reverse lookup — by grid coordinate (prefix 'g')
dom-density-map --at g48,40 --cols 60

# Custom CDP port (default: 9222)
dom-density-map --port 9515 https://example.com

# Works with python -m too
python -m dom_density_map --sparse --cols 60
```

## Output Modes

### Default Mode

Full character grid with column rulers. Each cell represents a pixel region:

```
=== DOM Density Map ===
Page: Example Domain
Viewport: 1920x1080  Grid: 80x45 (24px/cell)
Elements: 42 visible, 5 interactive

Legend: (space)=empty .=1elem :=2-3 #=4-7 @=8+
        B=button L=link F=input I=image/video T=text

0         1         2         3
0123456789012345678901234567890123456789
LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL
::::::::::::::::::::::::::::::::::::::::
TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT

--- Interactive (5) ---
L1: "More information..." at grid(40,22) px(980,540)
```

### Sparse Mode (`--sparse`)

RLE-compressed with row deduplication — minimal tokens for LLM consumption:

```
=== DOM Density Map (sparse) ===
Page: Example Domain
Viewport: 1920x1080  Grid: 60x34 (32px/cell)
Elements: 42 visible, 5 interactive
Key: _=empty .=1 :=2-3 #=4-7 @=8+ B=btn L=link F=input I=img T=text
RLE: X5 = XXXXX

r0: L60
r1-r3: :60  (x3)
r4-r8: T60  (x5)
r9-r33: (empty)

--- Interactive (5) ---
L1: "More information..." (980,540)
```

### Reverse Lookup (`--at`)

Full DOM stack at a specific point — tag, classes, attributes, state, and styles:

```
=== Elements at px(694,584) ===
Stack depth: 6

[0] <button> data-e2e="like-icon"
     class: btn-like active
     aria-pressed: true
     cursor: pointer
     bg: rgb(254, 44, 85)
     rect: (680,570) 28x28
[1] <div>
     class: action-bar
     rect: (670,520) 48x200
...
```

## Python API

```python
import asyncio
from dom_density_map import CDP, get_ws_url, render_sparse_map

async def main():
    ws_url = get_ws_url(port=9222)
    cdp = CDP(ws_url)
    await cdp.connect()

    # Navigate
    await cdp.navigate("https://example.com", wait=3)

    # Run DOM walker
    result = await cdp.execute_js("""
        // ... DOM_WALKER_JS is available as dom_density_map.core.DOM_WALKER_JS
    """)
    data = result.get("result", {}).get("value")

    # Render
    output = render_sparse_map(data, max_cols=60)
    print(output)

    await cdp.close()

asyncio.run(main())
```

Or use the JS constants directly:

```python
from dom_density_map.core import DOM_WALKER_JS, ELEMENTS_AT_JS
```

## Token Comparison

| Method | Tokens | Info |
|--------|--------|------|
| Screenshot (base64 PNG) | ~2,300 | Visual only, no selectors |
| `dom-density-map` (default) | ~1,200 | Layout + all interactive elements |
| `dom-density-map --sparse` | ~420 | RLE-compressed, same info |
| `dom-density-map --at X,Y` | ~550 | Full DOM stack at a point |

## Element Types

| Char | Block | Meaning |
|------|-------|---------|
| `B` | `\u25a3` | Button (`<button>`, `role="button"`, submit/reset inputs) |
| `F` | `\u25a4` | Form input (`<input>`, `<textarea>`, `<select>`, contenteditable) |
| `L` | `\u25a8` | Link (`<a href="...">`) |
| `I` | `\u25a7` | Image/media (`<img>`, `<video>`, `<canvas>`, `<svg>`) |
| `T` | `\u25a5` | Text block (element with >20 chars of direct text) |
| `.` | `\u2591` | 1 element |
| `:` | `\u2592` | 2-3 elements |
| `#` | `\u2593` | 4-7 elements |
| `@` | `\u2588` | 8+ elements |

## License

MIT
