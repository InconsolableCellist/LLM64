"""PNG -> C64 standard hires bitmap converter.

Converts any PIL-openable image to the C64's 320x200 hires bitmap mode:
the screen is 40x25 cells of 8x8 pixels, and each cell may use exactly two
colors from the fixed 16-color palette (foreground on bit=1, background on
bit=0, packed into screen RAM as (fg << 4) | bg).

Pipeline per image:
  1. letterbox to 320x200 (aspect preserved, black bars)
  2. per cell, pick the fg/bg pair that best covers the cell's pixels --
     candidate error is distance to the *line segment* between the pair in
     RGB space, since dithering can mix the two colors spatially
  3. one Floyd-Steinberg pass over the whole image, each pixel snapping to
     its cell's fg or bg, error diffusing across cell boundaries

render_preview() is the exact inverse, for verification without a C64.
"""

from PIL import Image

# Pepto's measured C64 palette (colodore.com lineage), indexed 0-15:
# black, white, red, cyan, purple, green, blue, yellow,
# orange, brown, light red, dark grey, grey, light green, light blue, light grey
PALETTE = [
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0x68, 0x37, 0x2B), (0x70, 0xA4, 0xB2),
    (0x6F, 0x3D, 0x86), (0x58, 0x8D, 0x43), (0x35, 0x28, 0x79), (0xB8, 0xC7, 0x6F),
    (0x6F, 0x4F, 0x25), (0x43, 0x39, 0x00), (0x9A, 0x67, 0x59), (0x44, 0x44, 0x44),
    (0x6C, 0x6C, 0x6C), (0x9A, 0xD2, 0x84), (0x6C, 0x5E, 0xB5), (0x95, 0x95, 0x95),
]

WIDTH, HEIGHT = 320, 200
CELLS_X, CELLS_Y = 40, 25

# Perceptual weights for RGB distance (roughly luma-proportional)
_WR, _WG, _WB = 3, 6, 1


def _dist2(r, g, b, c):
    dr, dg, db = r - c[0], g - c[1], b - c[2]
    return _WR * dr * dr + _WG * dg * dg + _WB * db * db


def trim_border(img, tolerance=12):
    """Crop away uniform border bands (image models love drawing frames
    and letterbox bars, which then poison the levels stretch)."""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()

    def row_uniform(y):
        r0, g0, b0 = px[w // 2, y]
        return all(abs(px[x, y][0] - r0) + abs(px[x, y][1] - g0)
                   + abs(px[x, y][2] - b0) <= tolerance * 3
                   for x in range(0, w, max(1, w // 64)))

    def col_uniform(x):
        r0, g0, b0 = px[x, h // 2]
        return all(abs(px[x, y][0] - r0) + abs(px[x, y][1] - g0)
                   + abs(px[x, y][2] - b0) <= tolerance * 3
                   for y in range(0, h, max(1, h // 64)))

    top = 0
    while top < h // 3 and row_uniform(top):
        top += 1
    bottom = h
    while bottom > h * 2 // 3 and row_uniform(bottom - 1):
        bottom -= 1
    left = 0
    while left < w // 3 and col_uniform(left):
        left += 1
    right = w
    while right > w * 2 // 3 and col_uniform(right - 1):
        right -= 1
    if top or left or bottom < h or right < w:
        img = img.crop((left, top, right, bottom))
    return img


def _letterbox(img):
    """Fit img into 320x200 preserving aspect, black bars on the short axis."""
    img = img.convert("RGB")
    scale = min(WIDTH / img.width, HEIGHT / img.height)
    w = max(1, round(img.width * scale))
    h = max(1, round(img.height * scale))
    img = img.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    canvas.paste(img, ((WIDTH - w) // 2, (HEIGHT - h) // 2))
    return canvas


def _seg_error(px, a, b):
    """Sum of squared distances from pixels to segment a-b in RGB space.

    Models what dithering can achieve: any color on the line between the
    pair is reachable by spatial mixing.
    """
    ax, ay, az = a
    dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
    seg2 = _WR * dx * dx + _WG * dy * dy + _WB * dz * dz
    total = 0
    for r, g, b_ in px:
        px_, py_, pz_ = r - ax, g - ay, b_ - az
        if seg2 == 0:
            t = 0.0
        else:
            t = (_WR * px_ * dx + _WG * py_ * dy + _WB * pz_ * dz) / seg2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
        er, eg, eb = px_ - t * dx, py_ - t * dy, pz_ - t * dz
        total += _WR * er * er + _WG * eg * eg + _WB * eb * eb
    return total


def _pick_pair(px):
    """Choose (fg, bg) palette indexes for one cell's 64 pixels."""
    # Candidates: every color that is nearest or second-nearest to some pixel
    cand = set()
    for r, g, b in px:
        best, best_d, second, second_d = 0, None, 0, None
        for i, c in enumerate(PALETTE):
            d = _dist2(r, g, b, c)
            if best_d is None or d < best_d:
                second, second_d = best, best_d
                best, best_d = i, d
            elif second_d is None or d < second_d:
                second, second_d = i, d
        cand.add(best)
        cand.add(second)
    cand = sorted(cand)

    best_pair, best_err = (cand[0], cand[0]), None
    for i in range(len(cand)):
        for j in range(i, len(cand)):
            err = _seg_error(px, PALETTE[cand[i]], PALETTE[cand[j]])
            if best_err is None or err < best_err:
                best_pair, best_err = (cand[i], cand[j]), err
    fg, bg = best_pair
    return fg, bg


def auto_levels(img, low_pct=2, high_pct=98, gamma=0.85, saturation=1.25):
    """Contrast/brightness normalization before dithering.

    AI generations skew dark and low-contrast for this palette; stretch
    the 2-98 percentile luminance range to full scale, brighten mids
    (gamma < 1), and boost saturation so colors land on distinct palette
    entries instead of all collapsing into the greys.
    """
    from PIL import ImageEnhance
    img = img.convert("RGB")
    hist = img.convert("L").histogram()
    total = sum(hist)
    lo, hi, acc = 0, 255, 0
    for i, n in enumerate(hist):
        acc += n
        if acc >= total * low_pct / 100:
            lo = i
            break
    acc = 0
    for i in range(255, -1, -1):
        acc += hist[i]
        if acc >= total * (100 - high_pct) / 100:
            hi = i
            break
    if hi <= lo:
        lo, hi = 0, 255
    span = hi - lo
    lut = [min(255, max(0, round(
        255 * ((min(max(v - lo, 0), span) / span) ** gamma))))
        for v in range(256)]
    img = img.point(lut * 3)
    return ImageEnhance.Color(img).enhance(saturation)


def convert_to_c64(img):
    """Convert a PIL image to (bitmap, matrix).

    bitmap: 8000 bytes, C64 hires layout -- 8 consecutive bytes per 8x8
    cell, cells left-to-right then top-to-bottom, bit 7 is the leftmost
    pixel, bit=1 selects the foreground color.
    matrix: 1000 bytes, one per cell, (fg << 4) | bg -- the value written
    straight into screen RAM in bitmap mode.
    """
    img = _letterbox(img)
    pix = list(img.getdata())  # row-major (r, g, b) tuples

    # Pass 1: per-cell color pair
    pairs = []
    for cy in range(CELLS_Y):
        for cx in range(CELLS_X):
            cell = []
            for row in range(8):
                base = (cy * 8 + row) * WIDTH + cx * 8
                cell.extend(pix[base:base + 8])
            pairs.append(_pick_pair(cell))

    # Pass 2: Floyd-Steinberg over the full image, snapping each pixel to
    # its cell's fg or bg; quantization error crosses cell boundaries.
    bitmap = bytearray(8000)
    err_cur = [(0.0, 0.0, 0.0)] * WIDTH
    err_next = [(0.0, 0.0, 0.0)] * WIDTH
    for y in range(HEIGHT):
        err_cur, err_next = err_next, [(0.0, 0.0, 0.0)] * WIDTH
        cy = y >> 3
        for x in range(WIDTH):
            r0, g0, b0 = pix[y * WIDTH + x]
            er, eg, eb = err_cur[x]
            r = min(255.0, max(0.0, r0 + er))
            g = min(255.0, max(0.0, g0 + eg))
            b = min(255.0, max(0.0, b0 + eb))
            fg, bg = pairs[cy * CELLS_X + (x >> 3)]
            cf, cb = PALETTE[fg], PALETTE[bg]
            use_fg = _dist2(r, g, b, cf) <= _dist2(r, g, b, cb)
            c = cf if use_fg else cb
            if use_fg:
                cell_i = cy * CELLS_X + (x >> 3)
                bitmap[cell_i * 8 + (y & 7)] |= 0x80 >> (x & 7)
            qr, qg, qb = r - c[0], g - c[1], b - c[2]
            if x + 1 < WIDTH:
                er, eg, eb = err_cur[x + 1]
                err_cur[x + 1] = (er + qr * 7 / 16, eg + qg * 7 / 16, eb + qb * 7 / 16)
            if y + 1 < HEIGHT:
                if x > 0:
                    er, eg, eb = err_next[x - 1]
                    err_next[x - 1] = (er + qr * 3 / 16, eg + qg * 3 / 16, eb + qb * 3 / 16)
                er, eg, eb = err_next[x]
                err_next[x] = (er + qr * 5 / 16, eg + qg * 5 / 16, eb + qb * 5 / 16)
                if x + 1 < WIDTH:
                    er, eg, eb = err_next[x + 1]
                    err_next[x + 1] = (er + qr * 1 / 16, eg + qg * 1 / 16, eb + qb * 1 / 16)

    matrix = bytes((fg << 4) | bg for fg, bg in pairs)
    return bytes(bitmap), matrix


MC_WIDTH = 160  # multicolor pixels are 2 hires pixels wide


def _pick_triple(px, bg):
    """Choose 3 free palette colors for one multicolor cell (4th is the
    global background). px is the cell's 32 (r,g,b) tuples."""
    from itertools import combinations
    freq = {}
    for r, g, b_ in px:
        best, best_d = 0, None
        for i, c in enumerate(PALETTE):
            d = _dist2(r, g, b_, c)
            if best_d is None or d < best_d:
                best, best_d = i, d
        freq[best] = freq.get(best, 0) + 1
    cand = sorted(freq, key=freq.get, reverse=True)[:10]
    if bg in cand:
        cand.remove(bg)
    if not cand:
        return (bg, bg, bg)
    while len(cand) < 3:
        cand.append(cand[0])

    best_triple, best_err = None, None
    for triple in combinations(cand, 3) if len(cand) >= 3 \
            else [tuple(cand[:3])]:
        colors = [PALETTE[bg]] + [PALETTE[i] for i in triple]
        err = 0
        for r, g, b_ in px:
            err += min(_dist2(r, g, b_, c) for c in colors)
            if best_err is not None and err >= best_err:
                break
        if best_err is None or err < best_err:
            best_triple, best_err = triple, err
    return best_triple


def convert_to_c64_mc(img, levels=True):
    """Convert a PIL image to C64 multicolor bitmap format.

    Returns (bitmap 8000, screen 1000, colram 1000, bg byte). 160x200
    fat pixels, each 2 bits: %00 = global background ($D021), %01 =
    screen RAM upper nibble, %10 = lower nibble, %11 = color RAM.
    Byte layout matches hires (8 bytes per 4x8-mc-pixel cell); each byte
    holds 4 pixels, most significant pair leftmost.
    """
    img = trim_border(img)
    if levels:
        img = auto_levels(img)
    img = _letterbox(img).resize((MC_WIDTH, HEIGHT), Image.LANCZOS)
    pix = list(img.getdata())

    # Global background: the most common nearest-palette color
    freq = {}
    for r, g, b in pix[::7]:  # sampled; exact counts don't matter
        best, best_d = 0, None
        for i, c in enumerate(PALETTE):
            d = _dist2(r, g, b, c)
            if best_d is None or d < best_d:
                best, best_d = i, d
        freq[best] = freq.get(best, 0) + 1
    bg = max(freq, key=freq.get)

    triples = []
    for cy in range(CELLS_Y):
        for cx in range(CELLS_X):
            cell = []
            for row in range(8):
                base = (cy * 8 + row) * MC_WIDTH + cx * 4
                cell.extend(pix[base:base + 4])
            triples.append(_pick_triple(cell, bg))

    bitmap = bytearray(8000)
    err_cur = [(0.0, 0.0, 0.0)] * MC_WIDTH
    err_next = [(0.0, 0.0, 0.0)] * MC_WIDTH
    for y in range(HEIGHT):
        err_cur, err_next = err_next, [(0.0, 0.0, 0.0)] * MC_WIDTH
        cy = y >> 3
        for x in range(MC_WIDTH):
            r0, g0, b0 = pix[y * MC_WIDTH + x]
            er, eg, eb = err_cur[x]
            r = min(255.0, max(0.0, r0 + er))
            g = min(255.0, max(0.0, g0 + eg))
            b = min(255.0, max(0.0, b0 + eb))
            cell_i = cy * CELLS_X + (x >> 2)
            codes = (bg,) + triples[cell_i]
            best_code, best_d = 0, None
            for code, pi in enumerate(codes):
                d = _dist2(r, g, b, PALETTE[pi])
                if best_d is None or d < best_d:
                    best_code, best_d = code, d
            c = PALETTE[codes[best_code]]
            if best_code:
                shift = (3 - (x & 3)) * 2
                bitmap[cell_i * 8 + (y & 7)] |= best_code << shift
            qr, qg, qb = r - c[0], g - c[1], b - c[2]
            if x + 1 < MC_WIDTH:
                er, eg, eb = err_cur[x + 1]
                err_cur[x + 1] = (er + qr * 7 / 16, eg + qg * 7 / 16,
                                  eb + qb * 7 / 16)
            if y + 1 < HEIGHT:
                if x > 0:
                    er, eg, eb = err_next[x - 1]
                    err_next[x - 1] = (er + qr * 3 / 16, eg + qg * 3 / 16,
                                      eb + qb * 3 / 16)
                er, eg, eb = err_next[x]
                err_next[x] = (er + qr * 5 / 16, eg + qg * 5 / 16,
                               eb + qb * 5 / 16)
                if x + 1 < MC_WIDTH:
                    er, eg, eb = err_next[x + 1]
                    err_next[x + 1] = (er + qr / 16, eg + qg / 16,
                                      eb + qb / 16)

    screen = bytes((t[0] << 4) | t[1] for t in triples)
    colram = bytes(t[2] for t in triples)
    return bytes(bitmap), screen, colram, bg


def render_preview_mc(bitmap, screen, colram, bg):
    """Inverse of convert_to_c64_mc (fat pixels doubled to 320 wide)."""
    img = Image.new("RGB", (WIDTH, HEIGHT))
    put = img.putpixel
    for cell_i in range(1000):
        cy, cx = divmod(cell_i, CELLS_X)
        colors = (PALETTE[bg], PALETTE[screen[cell_i] >> 4],
                  PALETTE[screen[cell_i] & 0x0F],
                  PALETTE[colram[cell_i] & 0x0F])
        for row in range(8):
            byte = bitmap[cell_i * 8 + row]
            y = cy * 8 + row
            for px_i in range(4):
                code = (byte >> ((3 - px_i) * 2)) & 3
                x = (cx * 4 + px_i) * 2
                put((x, y), colors[code])
                put((x + 1, y), colors[code])
    return img


def render_preview(bitmap, matrix):
    """Inverse of convert_to_c64: render bitmap+matrix to a PIL image."""
    if len(bitmap) != 8000 or len(matrix) != 1000:
        raise ValueError("expected 8000-byte bitmap and 1000-byte matrix")
    img = Image.new("RGB", (WIDTH, HEIGHT))
    put = img.putpixel
    for cell_i in range(1000):
        cy, cx = divmod(cell_i, CELLS_X)
        fg = PALETTE[matrix[cell_i] >> 4]
        bg = PALETTE[matrix[cell_i] & 0x0F]
        for row in range(8):
            byte = bitmap[cell_i * 8 + row]
            y = cy * 8 + row
            for bit in range(8):
                put((cx * 8 + bit, y), fg if byte & (0x80 >> bit) else bg)
    return img
