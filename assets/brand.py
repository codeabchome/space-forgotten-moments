from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math, random, os

F = "/home/claude/fonts/inter_extract/extras/ttf/"
OUT = "/mnt/user-data/outputs"
os.makedirs(OUT, exist_ok=True)

NAVY      = (10, 17, 40)
NAVY_DEEP = (6, 11, 28)
NAVY_MID  = (18, 28, 58)
SILVER    = (199, 204, 214)
SILVER_HI = (231, 234, 240)
STEEL     = (124, 132, 148)
STEEL_DK  = (58, 66, 84)

def font(name, size):
    return ImageFont.truetype(F + name, size)

def starfield(draw, w, h, n, seed=7, maxr=2.2, alpha_range=(40, 190)):
    rnd = random.Random(seed)
    for _ in range(n):
        x = rnd.uniform(0, w); y = rnd.uniform(0, h)
        r = rnd.uniform(0.5, maxr)
        a = rnd.randint(*alpha_range)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=SILVER_HI + (a,))

def dashed_ellipse(draw, cx, cy, rx, ry, rot, color, width, dash=14, gap=18, steps=1400):
    rot = math.radians(rot)
    pts = []
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        x = rx * math.cos(t); y = ry * math.sin(t)
        pts.append((cx + x * math.cos(rot) - y * math.sin(rot),
                    cy + x * math.sin(rot) + y * math.cos(rot)))
    # walk perimeter drawing dashes
    acc = 0.0; drawing = True; seg = []
    for i in range(len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        d = math.dist(p, q)
        if drawing:
            seg.append(p)
        acc += d
        limit = dash if drawing else gap
        if acc >= limit:
            if drawing and len(seg) > 1:
                draw.line(seg, fill=color, width=width, joint="curve")
            seg = []; drawing = not drawing; acc = 0.0
    if drawing and len(seg) > 1:
        draw.line(seg, fill=color, width=width)

def planet(base, cx, cy, r):
    """Silver sphere with soft terminator shading."""
    ss = 4
    size = int(r * 2 * ss) + 8
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    R = r * ss
    d.ellipse([c - R, c - R, c + R, c + R], fill=SILVER + (255,))
    # shading: darker crescent lower-right
    shade = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shade)
    off = R * 0.42
    ds.ellipse([c - R + off, c - R + off * 0.75, c + R + off, c + R + off * 0.75],
               fill=STEEL_DK + (150,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([c - R, c - R, c + R, c + R], fill=255)
    shade = shade.filter(ImageFilter.GaussianBlur(R * 0.22))
    layer = Image.composite(Image.alpha_composite(layer, shade), layer, mask)
    # highlight upper-left
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dh = ImageDraw.Draw(hl)
    dh.ellipse([c - R * 0.75, c - R * 0.8, c + R * 0.15, c - R * 0.05],
               fill=SILVER_HI + (110,))
    hl = hl.filter(ImageFilter.GaussianBlur(R * 0.28))
    layer = Image.composite(Image.alpha_composite(layer, hl), layer, mask)
    layer = layer.resize((int(size / ss), int(size / ss)), Image.LANCZOS)
    base.alpha_composite(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)))

def signal_trail(draw, x0, y0, dx, dy, n, r0, spacing):
    """Fading dot trail = the 'lost signal' motif."""
    for i in range(n):
        t = i / max(1, n - 1)
        x = x0 + dx * i * spacing
        y = y0 + dy * i * spacing
        r = r0 * (1 - 0.75 * t)
        a = int(230 * (1 - t) ** 1.5)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=STEEL + (a,))


# ---------------------------------------------------------------- PROFILE
def build_profile(path, S=800):
    img = Image.new("RGBA", (S, S), NAVY + (255,))
    # subtle radial vignette
    vig = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dv = ImageDraw.Draw(vig)
    for i in range(60):
        t = i / 60
        rr = S * (0.72 + 0.45 * t)
        dv.ellipse([S/2 - rr/2, S/2 - rr/2, S/2 + rr/2, S/2 + rr/2],
                   outline=NAVY_DEEP + (10,), width=6)
    img.alpha_composite(vig)

    # everything must live inside YouTube's circular crop (r = S/2),
    # with margin so nothing kisses the edge -> keep content within r ~ 0.44*S
    cx = cy = S / 2

    ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    starfield(d, S, S, 60, seed=11, maxr=2.0, alpha_range=(50, 165))
    dashed_ellipse(d, cx, cy, 268, 97, -20, STEEL + (165,), 6, dash=26, gap=30)
    dashed_ellipse(d, cx, cy, 205, 74, -20, STEEL_DK + (200,), 5, dash=16, gap=24)
    img.alpha_composite(ov)

    planet(img, cx, cy, 132)

    ov2 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(ov2)
    signal_trail(d2, cx + 212, cy + 90, 1.0, 0.36, 5, 14, 30)
    img.alpha_composite(ov2)

    img.convert("RGB").save(path, "PNG")
    return path


def circle_crop(src, dst, S=800):
    """Preview how YouTube renders the avatar (circular mask)."""
    im = Image.open(src).convert("RGBA").resize((S, S), Image.LANCZOS)
    mask = Image.new("L", (S * 4, S * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, S * 4, S * 4], fill=255)
    mask = mask.resize((S, S), Image.LANCZOS)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    out.save(dst, "PNG")
    return dst


# ---------------------------------------------------------------- BANNER
def build_banner(path, W=2560, H=1440):
    img = Image.new("RGBA", (W, H), NAVY + (255,))
    # vertical tonal wash: deeper at edges
    wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dw = ImageDraw.Draw(wash)
    for y in range(H):
        t = abs(y - H / 2) / (H / 2)
        dw.line([(0, y), (W, y)], fill=NAVY_DEEP + (int(120 * t ** 1.6),))
    img.alpha_composite(wash)

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    starfield(d, W, H, 320, seed=3, maxr=2.4, alpha_range=(30, 175))

    # orbit system pushed right so it never crowds the text column
    cx, cy = W * 0.862, H * 0.5
    p_r = 196
    dashed_ellipse(d, cx, cy, 540, 194, -20, STEEL_DK + (170,), 5, dash=30, gap=36)
    dashed_ellipse(d, cx, cy, 412, 148, -20, STEEL + (110,), 4, dash=20, gap=30)
    img.alpha_composite(ov)

    planet(img, cx, cy, p_r)

    ov2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(ov2)
    signal_trail(d2, cx + 286, cy + 122, 1.0, 0.36, 7, 17, 40)
    img.alpha_composite(ov2)

    d3 = ImageDraw.Draw(img)
    # ---- safe area (visible on every device): 1546 x 423, centered ----
    sx = (W - 1546) / 2      # 507
    sy = (H - 423) / 2       # 508.5
    tx = sx + 30
    # text column must clear the planet's left edge with breathing room
    col_w = (cx - p_r - 70) - tx

    def fit(text, ttf, start, max_w):
        s = start
        while s > 40:
            f = font(ttf, s)
            if d3.textlength(text, font=f) <= max_w:
                return f
            s -= 2
        return font(ttf, 40)

    f_title = fit("FORGOTTEN MOMENTS", "InterDisplay-SemiBold.ttf", 130, col_w)
    f_sub   = font("Inter-Regular.ttf", 40)
    f_kick  = font("Inter-Medium.ttf", 28)

    kick = "F R O M   T H E   N A S A   A R C H I V E"
    d3.text((tx, sy + 22), kick, font=f_kick, fill=STEEL)
    kw = d3.textlength(kick, font=f_kick)
    d3.line([(tx, sy + 66), (tx + kw, sy + 66)], fill=STEEL_DK, width=2)

    lh = int(f_title.size * 1.02)
    d3.text((tx, sy + 100), "SPACE'S", font=f_title, fill=SILVER_HI)
    d3.text((tx, sy + 100 + lh), "FORGOTTEN MOMENTS", font=f_title, fill=SILVER_HI)

    d3.text((tx + 3, sy + 368),
            "Missions history left behind  ·  New episodes 2–3× weekly",
            font=f_sub, fill=STEEL)

    img.convert("RGB").save(path, "PNG")
    return path


p1 = build_profile(f"{OUT}/sfm_profile_800.png")
p2 = build_banner(f"{OUT}/sfm_banner_2560x1440.png")

p3 = circle_crop(p1, f"{OUT}/sfm_profile_circle_preview.png")

for p in (p1, p2, p3):
    im = Image.open(p)
    print(p, im.size, f"{os.path.getsize(p)/1024:.0f} KB")
