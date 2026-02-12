"""DOM Density Map — Text-based page layout for LLM consumption.

Connects to a CDP Chrome instance, walks the visible DOM, and renders
a character grid showing element density + type hints. Interactive
elements are indexed with labels and coordinates.

Usage:
    dom-density-map                          # Map current page (port 9222)
    dom-density-map <url>                    # Navigate + map
    dom-density-map --cols 120               # Custom grid width
    dom-density-map --blocks                 # Unicode block art mode
    dom-density-map --at 694,584             # Reverse lookup at pixel coords
    dom-density-map --at g48,40              # Reverse lookup at grid coords (prefix 'g')
    dom-density-map --sparse                 # RLE + row dedup (minimal tokens)
    dom-density-map --port 9515              # Custom CDP port
"""

import asyncio
import sys

from .cdp import CDP, get_ws_url, is_chrome_running

# ---------------------------------------------------------------------------
# Stage 1: JavaScript DOM walker (runs in browser via Runtime.evaluate)
# ---------------------------------------------------------------------------

DOM_WALKER_JS = r"""
(function() {
    var vw = window.innerWidth, vh = window.innerHeight;
    var all = document.querySelectorAll('*');
    var elems = [];
    var CAP = 2000;

    for (var i = 0; i < all.length && elems.length < CAP; i++) {
        var el = all[i];
        var st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (parseFloat(st.opacity) === 0) continue;

        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;

        // Clamp to viewport
        var x = Math.max(0, r.left);
        var y = Math.max(0, r.top);
        var w = Math.min(r.right, vw) - x;
        var h = Math.min(r.bottom, vh) - y;
        if (w <= 0 || h <= 0) continue;

        // Classify element type (first match)
        var tag = el.tagName.toLowerCase();
        var role = el.getAttribute('role');
        var k = null;  // kind
        var isInteractive = false;

        if (tag === 'button' || role === 'button' ||
            (tag === 'input' && (el.type === 'button' || el.type === 'submit' || el.type === 'reset'))) {
            k = 'B'; isInteractive = true;
        } else if (tag === 'input' || tag === 'textarea' || tag === 'select' ||
                   el.contentEditable === 'true' || role === 'textbox') {
            k = 'F'; isInteractive = true;
        } else if (tag === 'a' && el.href) {
            k = 'L'; isInteractive = true;
        } else if (tag === 'img' || tag === 'video' || tag === 'canvas' || tag === 'svg') {
            k = 'I';
        } else {
            // Check for text content (direct text nodes only)
            var directText = '';
            for (var c = 0; c < el.childNodes.length; c++) {
                if (el.childNodes[c].nodeType === 3) {
                    directText += el.childNodes[c].textContent;
                }
            }
            if (directText.trim().length > 20) {
                k = 'T';
            }
        }

        var entry = {x: Math.round(x), y: Math.round(y),
                     w: Math.round(w), h: Math.round(h)};
        if (k) entry.k = k;

        // Capture label for interactive elements
        if (isInteractive) {
            entry.i = true;
            var label = el.getAttribute('aria-label') ||
                        (el.textContent || '').trim().substring(0, 60) ||
                        el.title || el.placeholder || '';
            label = label.replace(/\s+/g, ' ').trim();
            if (label.length > 60) label = label.substring(0, 57) + '...';
            if (label) entry.l = label;
        }

        elems.push(entry);
    }

    return {vw: vw, vh: vh, count: elems.length, elements: elems};
})()
"""

ELEMENTS_AT_JS = r"""
(function(px, py) {
    var els = document.elementsFromPoint(px, py);
    var results = [];
    for (var i = 0; i < els.length && results.length < 15; i++) {
        var el = els[i];
        var tag = el.tagName.toLowerCase();
        var r = el.getBoundingClientRect();
        var entry = {
            tag: tag,
            rect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)}
        };

        // Attributes worth showing
        if (el.id) entry.id = el.id;
        var cls = el.className;
        if (typeof cls === 'string' && cls.trim()) {
            entry.cls = cls.trim().split(/\s+/).slice(0, 4).join(' ');
        }
        var role = el.getAttribute('role');
        if (role) entry.role = role;
        var de = el.getAttribute('data-e2e');
        if (de) entry.data_e2e = de;
        var aria = el.getAttribute('aria-label');
        if (aria) entry.aria = aria.substring(0, 80);
        var href = el.getAttribute('href');
        if (href) entry.href = href.substring(0, 120);

        // State
        if (el.getAttribute('aria-pressed')) entry.pressed = el.getAttribute('aria-pressed');
        if (el.contentEditable === 'true') entry.editable = true;
        if (el.disabled) entry.disabled = true;

        // Direct text (first 80 chars)
        var txt = '';
        for (var c = 0; c < el.childNodes.length; c++) {
            if (el.childNodes[c].nodeType === 3) txt += el.childNodes[c].textContent;
        }
        txt = txt.trim();
        if (txt) entry.text = txt.substring(0, 80);

        // Computed style hints
        var st = window.getComputedStyle(el);
        if (st.cursor === 'pointer') entry.clickable = true;
        var bg = st.backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') entry.bg = bg;
        var color = st.color;
        if (color) entry.color = color;

        results.push(entry);
    }
    return results;
})(%d, %d)
"""


def render_elements_at(elements, px, py):
    """Format the elementsFromPoint result as readable text."""
    lines = []
    lines.append(f"=== Elements at px({px},{py}) ===")
    lines.append(f"Stack depth: {len(elements)}")
    lines.append("")
    for i, el in enumerate(elements):
        tag = el['tag']
        parts = [f"[{i}] <{tag}>"]
        if el.get('id'):
            parts.append(f'id="{el["id"]}"')
        if el.get('role'):
            parts.append(f'role="{el["role"]}"')
        if el.get('data_e2e'):
            parts.append(f'data-e2e="{el["data_e2e"]}"')
        lines.append(' '.join(parts))

        if el.get('cls'):
            lines.append(f"     class: {el['cls']}")
        if el.get('aria'):
            lines.append(f"     aria-label: \"{el['aria']}\"")
        if el.get('href'):
            lines.append(f"     href: {el['href']}")
        if el.get('text'):
            lines.append(f"     text: \"{el['text']}\"")
        if el.get('pressed'):
            lines.append(f"     aria-pressed: {el['pressed']}")
        if el.get('editable'):
            lines.append(f"     contentEditable: true")
        if el.get('disabled'):
            lines.append(f"     disabled: true")
        if el.get('clickable'):
            lines.append(f"     cursor: pointer")
        if el.get('bg'):
            lines.append(f"     bg: {el['bg']}")
        if el.get('color'):
            lines.append(f"     color: {el['color']}")
        r = el.get('rect', {})
        lines.append(f"     rect: ({r.get('x',0)},{r.get('y',0)}) {r.get('w',0)}x{r.get('h',0)}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Stage 2 & 3: Python grid renderer + interactive element index
# ---------------------------------------------------------------------------

# Type priority: higher wins cell ownership
_PRIORITY = {'T': 1, 'I': 2, 'L': 3, 'F': 4, 'B': 5}
# Density chars (when no type override)
_DENSITY_CHARS = {0: ' ', 1: '.', 2: ':', 3: ':', 4: '#', 5: '#', 6: '#', 7: '#'}


_BLOCK_DENSITY = {0: ' ', 1: '\u2591', 2: '\u2592', 3: '\u2592', 4: '\u2593', 5: '\u2593', 6: '\u2593', 7: '\u2593'}
_BLOCK_TYPES = {'B': '\u25a3', 'F': '\u25a4', 'L': '\u25a8', 'I': '\u25a7', 'T': '\u25a5'}


def _density_char(count, blocks=False):
    if blocks:
        if count == 0:
            return ' '
        if count == 1:
            return '\u2591'
        if count <= 3:
            return '\u2592'
        if count <= 7:
            return '\u2593'
        return '\u2588'
    if count == 0:
        return ' '
    if count == 1:
        return '.'
    if count <= 3:
        return ':'
    if count <= 7:
        return '#'
    return '@'


def render_density_map(data, title="", url="", max_cols=160, blocks=False):
    """Build and return the text density map from DOM walker data."""
    vw = data['vw']
    vh = data['vh']
    elements = data['elements']

    cols = min(max_cols, vw)
    cell_px = vw / cols
    rows = max(1, round(vh / cell_px))
    # Cap total cells
    if cols * rows > 16000:
        rows = 16000 // cols

    # Grid: density count per cell + type kind per cell
    density = [[0] * cols for _ in range(rows)]
    kinds = [[None] * cols for _ in range(rows)]

    interactive = []

    for el in elements:
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        kind = el.get('k')
        is_interactive = el.get('i', False)

        # Grid cell range this element spans
        c0 = int(ex / cell_px)
        c1 = int((ex + ew - 1) / cell_px)
        r0 = int(ey / cell_px)
        r1 = int((ey + eh - 1) / cell_px)

        c0 = max(0, min(c0, cols - 1))
        c1 = max(0, min(c1, cols - 1))
        r0 = max(0, min(r0, rows - 1))
        r1 = max(0, min(r1, rows - 1))

        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                density[r][c] += 1
                if kind:
                    cur = kinds[r][c]
                    if cur is None or _PRIORITY.get(kind, 0) > _PRIORITY.get(cur, 0):
                        kinds[r][c] = kind

        if is_interactive:
            # Center of element in grid coords and pixel coords
            gc = int((ex + ew / 2) / cell_px)
            gr = int((ey + eh / 2) / cell_px)
            interactive.append({
                'kind': kind,
                'label': el.get('l', ''),
                'gc': gc, 'gr': gr,
                'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
            })

    # Sort interactive: top-to-bottom, left-to-right
    interactive.sort(key=lambda e: (e['gr'], e['gc']))
    interactive = interactive[:50]

    # Count interactive elements
    n_interactive = len(interactive)

    # Build output
    lines = []
    lines.append("=== DOM Density Map ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    lines.append(f"Viewport: {vw}x{vh}  Grid: {cols}x{rows} ({cell_px:.0f}px/cell)")
    lines.append(f"Elements: {data['count']} visible, {n_interactive} interactive")
    lines.append("")
    if blocks:
        lines.append("Legend: (space)=empty \u2591=1 \u2592=2-3 \u2593=4-7 \u2588=8+")
        lines.append("        \u25a3=button \u25a8=link \u25a4=input \u25a7=image/video \u25a5=text")
    else:
        lines.append("Legend: (space)=empty .=1elem :=2-3 #=4-7 @=8+")
        lines.append("        B=button L=link F=input I=image/video T=text")
    lines.append("")

    # Column ruler (every 10)
    ruler_tens = ''.join([str((i // 10) % 10) if i % 10 == 0 else ' '
                          for i in range(cols)])
    ruler_ones = ''.join([str(i % 10) for i in range(cols)])
    lines.append(ruler_tens)
    lines.append(ruler_ones)

    # Grid rows
    for r in range(rows):
        row_chars = []
        for c in range(cols):
            k = kinds[r][c]
            d = density[r][c]
            if k:
                row_chars.append(_BLOCK_TYPES[k] if blocks else k)
            else:
                row_chars.append(_density_char(d, blocks=blocks))
        lines.append(''.join(row_chars))

    # Interactive element index
    if interactive:
        lines.append("")
        lines.append(f"--- Interactive ({n_interactive}) ---")
        # Assign sequential IDs per type
        type_counters = {}
        for item in interactive:
            k = item['kind'] or '?'
            type_counters[k] = type_counters.get(k, 0) + 1
            eid = f"{k}{type_counters[k]}"
            label = item['label']
            label_str = f' "{label}"' if label else ''
            lines.append(
                f"{eid}:{label_str} at grid({item['gc']},{item['gr']}) "
                f"px({item['px']},{item['py']})"
            )

    return '\n'.join(lines)


def _rle_row(chars):
    """Run-length encode a row of chars. 'BBB@@...' -> 'B3@2.2'"""
    if not chars:
        return ''
    parts = []
    cur = chars[0]
    count = 1
    for ch in chars[1:]:
        if ch == cur:
            count += 1
        else:
            parts.append(cur if count == 1 else f"{cur}{count}")
            cur = ch
            count = 1
    parts.append(cur if count == 1 else f"{cur}{count}")
    return ''.join(parts)


def render_sparse_map(data, title="", url="", max_cols=160, blocks=False):
    """Compressed density map: RLE rows + row deduplication."""
    vw = data['vw']
    vh = data['vh']
    elements = data['elements']

    cols = min(max_cols, vw)
    cell_px = vw / cols
    rows = max(1, round(vh / cell_px))
    if cols * rows > 16000:
        rows = 16000 // cols

    density = [[0] * cols for _ in range(rows)]
    kinds = [[None] * cols for _ in range(rows)]
    interactive = []

    for el in elements:
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        kind = el.get('k')
        is_interactive = el.get('i', False)

        c0 = max(0, min(int(ex / cell_px), cols - 1))
        c1 = max(0, min(int((ex + ew - 1) / cell_px), cols - 1))
        r0 = max(0, min(int(ey / cell_px), rows - 1))
        r1 = max(0, min(int((ey + eh - 1) / cell_px), rows - 1))

        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                density[r][c] += 1
                if kind:
                    cur = kinds[r][c]
                    if cur is None or _PRIORITY.get(kind, 0) > _PRIORITY.get(cur, 0):
                        kinds[r][c] = kind

        if is_interactive:
            gc = int((ex + ew / 2) / cell_px)
            gr = int((ey + eh / 2) / cell_px)
            interactive.append({
                'kind': kind, 'label': el.get('l', ''),
                'gc': gc, 'gr': gr,
                'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
            })

    interactive.sort(key=lambda e: (e['gr'], e['gc']))
    interactive = interactive[:50]

    # Build char rows
    char_rows = []
    for r in range(rows):
        row = []
        for c in range(cols):
            k = kinds[r][c]
            d = density[r][c]
            if k:
                row.append(_BLOCK_TYPES[k] if blocks else k)
            else:
                row.append(_density_char(d, blocks=blocks))
        char_rows.append(row)

    # RLE encode each row
    rle_rows = [_rle_row(row) for row in char_rows]

    # Build output
    lines = []
    lines.append(f"=== DOM Density Map (sparse) ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    lines.append(f"Viewport: {vw}x{vh}  Grid: {cols}x{rows} ({cell_px:.0f}px/cell)")
    lines.append(f"Elements: {data['count']} visible, {len(interactive)} interactive")
    lines.append(f"Key: _=empty .=1 :=2-3 #=4-7 @=8+ B=btn L=link F=input I=img T=text")
    lines.append(f"RLE: X5 = XXXXX")
    lines.append("")

    # Emit rows with dedup (collapse identical consecutive rows)
    i = 0
    while i < len(rle_rows):
        rle = rle_rows[i]
        # Find how many consecutive rows are identical
        j = i + 1
        while j < len(rle_rows) and rle_rows[j] == rle:
            j += 1
        count = j - i

        # Check if row is all empty
        is_empty = all(ch == ' ' for ch in char_rows[i])

        if is_empty:
            if count == 1:
                lines.append(f"r{i}: (empty)")
            else:
                lines.append(f"r{i}-{j-1}: (empty)")
        elif count == 1:
            lines.append(f"r{i}: {rle}")
        elif count == 2:
            lines.append(f"r{i}-{j-1}: {rle}")
        else:
            lines.append(f"r{i}-{j-1}: {rle}  (x{count})")
        i = j

    # Interactive element index
    if interactive:
        lines.append("")
        lines.append(f"--- Interactive ({len(interactive)}) ---")
        type_counters = {}
        for item in interactive:
            k = item['kind'] or '?'
            type_counters[k] = type_counters.get(k, 0) + 1
            eid = f"{k}{type_counters[k]}"
            label = item['label']
            label_str = f' "{label}"' if label else ''
            lines.append(
                f"{eid}:{label_str} ({item['px']},{item['py']})"
            )

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main: CDP connect, optional navigate, execute JS, render
# ---------------------------------------------------------------------------

async def run(args):
    url = None
    max_cols = 160
    blocks = False
    sparse = False
    at_coord = None  # (px, py) or ('g', col, row)
    port = 9222

    # Parse args
    i = 0
    while i < len(args):
        if args[i] == '--cols' and i + 1 < len(args):
            max_cols = int(args[i + 1])
            i += 2
        elif args[i] == '--blocks':
            blocks = True
            i += 1
        elif args[i] == '--sparse':
            sparse = True
            i += 1
        elif args[i] == '--port' and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == '--at' and i + 1 < len(args):
            raw = args[i + 1]
            if raw.startswith('g'):
                # Grid coords: g48,40
                parts = raw[1:].split(',')
                at_coord = ('g', int(parts[0]), int(parts[1]))
            else:
                # Pixel coords: 694,584
                parts = raw.split(',')
                at_coord = ('px', int(parts[0]), int(parts[1]))
            i += 2
        elif args[i] in ('-h', '--help'):
            print("Usage: dom-density-map [OPTIONS] [URL]")
            print()
            print("Options:")
            print("  --cols N      Grid width in columns (default: 160)")
            print("  --sparse      RLE-compressed output (minimal tokens)")
            print("  --blocks      Unicode block art mode")
            print("  --at X,Y      Reverse lookup at pixel coords")
            print("  --at gC,R     Reverse lookup at grid coords")
            print("  --port PORT   CDP port (default: 9222)")
            print("  -h, --help    Show this help")
            return
        elif not args[i].startswith('-'):
            url = args[i]
            i += 1
        else:
            i += 1

    if not is_chrome_running(port):
        print(
            f"Error: Chrome not found on port {port}.\n"
            f"Start Chrome with: google-chrome --remote-debugging-port={port}",
            file=sys.stderr,
        )
        sys.exit(1)

    ws_url = get_ws_url(port)
    cdp = CDP(ws_url)
    await cdp.connect()

    try:
        if url:
            await cdp.navigate(url, wait=3)

        # Get current page title and URL
        title_result = await cdp.execute_js("document.title")
        page_title = title_result.get("result", {}).get("value", "")
        url_result = await cdp.execute_js("window.location.href")
        page_url = url_result.get("result", {}).get("value", "")

        if at_coord:
            # Reverse lookup mode
            if at_coord[0] == 'g':
                # Convert grid coords to pixel coords using current viewport
                vw_result = await cdp.execute_js("window.innerWidth")
                vw = vw_result.get("result", {}).get("value", 1920)
                cell_px = vw / max_cols
                px = int(at_coord[1] * cell_px + cell_px / 2)
                py = int(at_coord[2] * cell_px + cell_px / 2)
            else:
                px, py = at_coord[1], at_coord[2]

            js = ELEMENTS_AT_JS % (px, py)
            result = await cdp.execute_js(js)
            elements = result.get("result", {}).get("value")
            if not elements:
                print(f"No elements found at px({px},{py})")
                return
            output = render_elements_at(elements, px, py)
            print(output)
            return

        # Run DOM walker
        result = await cdp.execute_js(DOM_WALKER_JS)
        data = result.get("result", {}).get("value")

        if not data:
            print("Error: DOM walker returned no data")
            return

        if sparse:
            output = render_sparse_map(data, title=page_title, url=page_url,
                                       max_cols=max_cols, blocks=blocks)
        else:
            output = render_density_map(data, title=page_title, url=page_url,
                                        max_cols=max_cols, blocks=blocks)
        print(output)

    finally:
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
