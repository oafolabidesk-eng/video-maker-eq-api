"""
mixing_scene.py - Manim Community Edition scene used by app.py.

Renders a hardware-style studio plugin faceplate (dark rack chassis, glowing LED,
12 selectable plugin presets, 3 rotary knobs, grid display with animated response
curve) with word-wrapped captions that follow an edge-tts WebVTT file.

Environment variables (all set by app.py):
    SCENE_TEXT        Full narration script (used as caption fallback)
    VTT_PATH          Path to edge-tts WebVTT subtitle file
    VISUAL_MODE       eq | compressor | reverb | delay | saturation | limiter |
                      deesser | imager | flanger | chorus | phaser | universal
    VIDEO_TYPE        "short" (9:16) or "long" (16:9)
    TEXT_X_PCT/_Y_PCT Caption anchor, normalized [0, 1] from the top-left corner
    AUDIO_DURATION    Seconds; the animation lasts exactly this long
    CURVE_POWER       Shape exponent. 2.0 is the neutral, as-designed shape;
                      higher values fatten/round the curve's features, lower
                      values sharpen them (deviation ** (2 / CURVE_POWER)).
    CURVE_GAIN        Amplitude multiplier of the curve (0.85 = 85% of the
                      screen's half-height at full deflection).
    CUSTOM_TITLE      Header title override
    CUSTOM_KNOBS      3 knob labels joined with "|"
    CUSTOM_X_LABELS   6 horizontal-axis labels joined with "|"
    CUSTOM_Y_LABELS   5 vertical-axis labels joined with "|"

Render (as app.py does):
    manim -ql --disable_caching -r 480,854 mixing_scene.py MixingGraph
No LaTeX is required; only Text objects are used.
"""

import math
import os
import re
import textwrap
from bisect import bisect_right
from typing import Callable, List, Optional, Tuple

import numpy as np
from manim import (
    BOLD,
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Circle,
    DashedLine,
    Dot,
    Line,
    RoundedRectangle,
    Scene,
    Text,
    ValueTracker,
    VGroup,
    VMobject,
    config,
    linear,
)

# Manim keeps frame_width at 14.22 and stretches the camera height when a custom
# -r WIDTH,HEIGHT is given, which would distort the vertical (9:16) layout.
# Pin the frame to 8 units tall and derive the width from the pixel aspect ratio.
config.frame_height = 8.0
config.frame_width = config.frame_height * config.pixel_width / config.pixel_height

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_labels(name: str, count: int) -> Optional[List[str]]:
    raw = os.environ.get(name)
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("|")]
    return parts if len(parts) == count and all(parts) else None


# ---------------------------------------------------------------------------
# Plugin presets
# ---------------------------------------------------------------------------

PRESETS = {
    "eq": dict(
        title="PRO-Q4 PARAMETRIC EQ", color="#00E5FF",
        knobs=["FREQ", "GAIN", "Q"],
        x=["20Hz", "100Hz", "500Hz", "2kHz", "8kHz", "20kHz"],
        y=["-12dB", "-6dB", "0dB", "+6dB", "+12dB"],
    ),
    "compressor": dict(
        title="1176 FET COMPRESSOR", color="#FFAB00",
        knobs=["THRESH", "RATIO", "RELEASE"],
        x=["-40dB", "-30dB", "-20dB", "-10dB", "-5dB", "0dB"],
        y=["-12dB", "-9dB", "-6dB", "-3dB", "0dB"],
    ),
    "reverb": dict(
        title="LEXI PLATE REVERB", color="#B388FF",
        knobs=["PRE-DLY", "DECAY", "MIX"],
        x=["0ms", "100ms", "300ms", "600ms", "1.2s", "2.5s"],
        y=["-60dB", "-45dB", "-30dB", "-15dB", "0dB"],
    ),
    "delay": dict(
        title="ECHOBOY TAPE DELAY", color="#00E676",
        knobs=["TIME", "FEEDBACK", "MIX"],
        x=["1/16", "1/8", "1/8D", "1/4", "1/2", "1/1"],
        y=["0%", "25%", "50%", "75%", "100%"],
    ),
    "saturation": dict(
        title="TUBE WARMTH SATURATOR", color="#FF6D00",
        knobs=["DRIVE", "HARMONICS", "OUTPUT"],
        x=["-24dB", "-18dB", "-12dB", "-6dB", "-3dB", "0dB"],
        y=["-12dB", "-6dB", "0dB", "+3dB", "+6dB"],
    ),
    "limiter": dict(
        title="L2 BRICKWALL LIMITER", color="#FF1744",
        knobs=["THRESH", "CEILING", "RELEASE"],
        x=["-18dB", "-12dB", "-9dB", "-6dB", "-3dB", "0dB"],
        y=["-12dB", "-6dB", "-3dB", "-1dB", "0dB"],
    ),
    "deesser": dict(
        title="S-CONTROL DE-ESSER", color="#F50057",
        knobs=["FREQ", "THRESH", "RANGE"],
        x=["2kHz", "4kHz", "6kHz", "8kHz", "10kHz", "14kHz"],
        y=["-12dB", "-9dB", "-6dB", "-3dB", "0dB"],
    ),
    "imager": dict(
        title="S1 STEREO IMAGER", color="#2979FF",
        knobs=["WIDTH", "ASYMMETRY", "MONO BASS"],
        x=["L100", "L50", "CENTER", "R50", "R100", "SIDE"],
        y=["-18dB", "-12dB", "-6dB", "-3dB", "0dB"],
    ),
    "flanger": dict(
        title="JETSTREAM FLANGER", color="#1DE9B6",
        knobs=["RATE", "DEPTH", "FEEDBACK"],
        x=["50Hz", "200Hz", "800Hz", "2kHz", "6kHz", "16kHz"],
        y=["-12dB", "-6dB", "0dB", "+6dB", "+12dB"],
    ),
    "chorus": dict(
        title="DIMENSION CHORUS", color="#40C4FF",
        knobs=["RATE", "DEPTH", "SPREAD"],
        x=["20Hz", "100Hz", "500Hz", "2kHz", "8kHz", "20kHz"],
        y=["-12dB", "-6dB", "0dB", "+6dB", "+12dB"],
    ),
    "phaser": dict(
        title="SMALL STONE PHASER", color="#C6FF00",
        knobs=["RATE", "COLOR", "STAGES"],
        x=["40Hz", "150Hz", "600Hz", "2kHz", "7kHz", "18kHz"],
        y=["-12dB", "-6dB", "0dB", "+6dB", "+12dB"],
    ),
    "universal": dict(
        title="VOCAL CHAIN ANALYZER", color="#FFD740",
        knobs=["TONE", "DYNAMICS", "SPACE"],
        x=["20Hz", "100Hz", "500Hz", "2kHz", "8kHz", "20kHz"],
        y=["-12dB", "-6dB", "0dB", "+6dB", "+12dB"],
    ),
}

# Normalized x position of each of the 6 axis ticks (slot centers).
TICKS_X = [(i + 0.5) / 6.0 for i in range(6)]

# ---------------------------------------------------------------------------
# Curve math. Every curve function returns (primary, ghost) arrays in
# "deviation space" [-1, 1] (0 = screen center). ``ghost`` is an optional
# dimmer reference trace. The shaping pipeline then applies CURVE_POWER/GAIN.
# ---------------------------------------------------------------------------


def _g(x, c, w):
    return np.exp(-(((x - c) / w) ** 2))


def _sig(z):
    return 1.0 / (1.0 + np.exp(-z))


def _interp_ext(x, xp, fp):
    """np.interp with linear extrapolation at both ends."""
    x = np.asarray(x, dtype=float)
    xp = np.asarray(xp, dtype=float)
    fp = np.asarray(fp, dtype=float)
    y = np.interp(x, xp, fp)
    left = (fp[1] - fp[0]) / (xp[1] - xp[0])
    right = (fp[-1] - fp[-2]) / (xp[-1] - xp[-2])
    y = np.where(x < xp[0], fp[0] + left * (x - xp[0]), y)
    y = np.where(x > xp[-1], fp[-1] + right * (x - xp[-1]), y)
    return y


def _to_dev(y01):
    return 2.0 * np.clip(y01, 0.0, 1.0) - 1.0


def curve_eq(x, t):
    c1 = 0.50 + 0.28 * math.sin(0.7 * t)
    a1 = 0.60 * math.sin(0.9 * t + 0.4)
    c2 = 0.72 + 0.12 * math.sin(0.5 * t + 2.0)
    a2 = 0.45 * math.sin(0.6 * t + 1.7)
    low_cut = -0.40 * (0.5 + 0.5 * math.sin(0.4 * t)) * _sig((0.16 - x) / 0.035)
    high_shelf = 0.30 * math.sin(0.35 * t + 1.0) * _sig((x - 0.84) / 0.04)
    d = a1 * _g(x, c1, 0.11) + a2 * _g(x, c2, 0.07) + low_cut + high_shelf
    return np.clip(d, -1, 1), np.zeros_like(x)


_COMP_FREQ_DB = [-40, -30, -20, -10, -5, 0]


def _comp_gr(in_db, t):
    thr = -18.0 + 6.0 * math.sin(0.6 * t)
    ratio = 4.0 + 1.5 * math.sin(0.45 * t + 1.0)
    knee = 6.0
    slope = 1.0 - 1.0 / ratio
    over = in_db - thr
    return np.where(
        2 * over < -knee,
        0.0,
        np.where(
            2 * np.abs(over) <= knee,
            slope * (over + knee / 2.0) ** 2 / (2.0 * knee),
            slope * over,
        ),
    )


def curve_compressor(x, t):
    in_db = _interp_ext(x, TICKS_X, _COMP_FREQ_DB)
    gr = _comp_gr(in_db, t)
    return _to_dev(1.0 - np.clip(gr / 12.0, 0, 1)), None


_REVERB_SECONDS = [0.0, 0.1, 0.3, 0.6, 1.2, 2.5]


def curve_reverb(x, t):
    sec = _interp_ext(x, TICKS_X, _REVERB_SECONDS)
    pre = 0.05 + 0.04 * math.sin(0.7 * t)
    rt = 1.0 + 0.45 * math.sin(0.5 * t + 0.6)
    env = np.clip(1.0 - (sec - pre) / rt, 0.0, 1.0)  # linear in dB = exponential decay
    ripple = 1.0 + 0.06 * np.sin(55.0 * x + 3.0 * t)
    y01 = _sig((sec - pre) / 0.012) * env * ripple
    dry = _g(x, 0.0, 0.012)
    return _to_dev(y01), _to_dev(dry)


def curve_delay(x, t):
    dt = 0.16 + 0.025 * math.sin(0.5 * t)
    fb = 0.72 + 0.10 * math.sin(0.7 * t + 0.8)
    y = np.zeros_like(x)
    for k in range(7):
        y += (fb ** k) * _g(x, 0.06 + k * dt, 0.011)
    env = np.where(x >= 0.06, fb ** ((x - 0.06) / dt), 0.0)
    return _to_dev(y), _to_dev(env)


def curve_saturation(x, t):
    drive = 1.5 + 3.0 * (0.5 + 0.5 * math.sin(0.6 * t))
    y = np.tanh(drive * x) / math.tanh(drive)
    y = y + 0.018 * np.sin(14.0 * x + 2.0 * t) * x * (drive - 1.0) / 3.0
    return _to_dev(y), _to_dev(x)


LIMITER_CEILING = 0.70  # screen position (0..1) of the -1dB tick


def curve_limiter(x, t):
    xt = 0.46 + 0.10 * math.sin(0.6 * t)
    base = 0.08
    line = base + (LIMITER_CEILING - base) * x / xt
    transient = 0.07 * _g(x, 0.78 + 0.10 * math.sin(2.0 * t), 0.02)
    y = np.minimum(line, LIMITER_CEILING)
    ghost = np.clip(line + transient, 0, 1)
    return _to_dev(y), _to_dev(ghost)


def curve_deesser(x, t):
    c = 0.50
    burst = (0.5 + 0.5 * math.sin(2.2 * t)) ** 2
    depth = 0.15 + 0.60 * burst
    notch = depth * _g(x, c + 0.015 * math.sin(1.1 * t), 0.07)
    y01 = 1.0 - notch - 0.04 * _sig((x - 0.85) / 0.05)
    ghost = 1.0 - 0.80 * _g(x, c, 0.07)
    return _to_dev(y01), _to_dev(ghost)


def curve_imager(x, t):
    c = TICKS_X[2]
    width = 0.06 + 0.05 * (0.5 + 0.5 * math.sin(0.9 * t))
    wide = 0.5 + 0.5 * math.sin(0.9 * t + 0.6)
    lobe = _g(x, c, width)
    sides = 0.65 * wide * (_g(x, TICKS_X[1], 0.05) + _g(x, TICKS_X[3], 0.05))
    far = 0.35 * wide * (_g(x, TICKS_X[0], 0.04) + _g(x, TICKS_X[4], 0.04))
    y01 = 0.12 + 0.80 * lobe + 0.70 * sides + 0.60 * far
    mono = 0.12 + 0.80 * _g(x, c, 0.03)
    return _to_dev(y01), _to_dev(mono)


def curve_flanger(x, t):
    k = 4.5 + 2.5 * math.sin(0.8 * t)
    fb = 0.45 + 0.25 * math.sin(0.5 * t + 1.0)
    comb = (0.5 + 0.5 * np.cos(2.0 * math.pi * k * x)) ** (1.0 + 1.5 * fb)
    d = (2.0 * comb - 1.0) * (0.9 - 0.25 * x)
    return np.clip(d, -1, 1), np.zeros_like(x)


def curve_chorus(x, t):
    env = 0.85 - 0.25 * x
    a = 0.45 * np.sin(2.0 * math.pi * 3.0 * x + 1.4 * t)
    detune = 1.0 + 0.06 * (0.5 + 0.5 * math.sin(0.8 * t))
    b = 0.45 * np.sin(2.0 * math.pi * 3.0 * detune * x - 1.1 * t + 0.6)
    return np.clip(0.8 * (a + b) * env, -1, 1), np.clip(b * env, -1, 1)


def curve_phaser(x, t):
    d = np.full_like(x, 0.35)
    for k in range(3):
        c = 0.2 + 0.2 * k + 0.08 * math.sin(0.9 * t + 1.1 * k) + 0.10 * math.sin(0.35 * t)
        d = d - 0.95 * _g(x, c, 0.03 + 0.01 * k)
    return np.clip(d, -1, 1), np.zeros_like(x)


_UNI_FREQS = [20.0, 100.0, 500.0, 2000.0, 8000.0, 20000.0]
_UNI_LOGF = np.log10(_UNI_FREQS)


def _uni_x_of_freq(f):
    return _interp_ext(np.log10(np.maximum(f, 1.0)), _UNI_LOGF, TICKS_X)


def _uni_formants(t):
    return [
        (700 + 180 * math.sin(0.9 * t), 0.09, 1.00),
        (1250 + 300 * math.sin(0.7 * t + 1.0), 0.08, 0.75),
        (2700 + 250 * math.sin(0.5 * t + 2.0), 0.07, 0.55),
        (4200 + 200 * math.sin(0.4 * t), 0.07, 0.35),
    ]


def curve_universal(x, t):
    formants = [(float(_uni_x_of_freq(np.array([f]))[0]), w, a) for f, w, a in _uni_formants(t)]
    norm = sum(a for _, _, a in formants)

    def envf(v):
        return sum(a * _g(v, c, w) for c, w, a in formants) / norm

    f0 = 110.0 + 14.0 * math.sin(0.8 * t)
    harm = np.zeros_like(x)
    for k in range(1, 60):
        f = k * f0
        if f > 20000.0:
            break
        xk = float(_uni_x_of_freq(np.array([f]))[0])
        amp = float(envf(np.array([xk]))[0]) / (k ** 0.25)
        harm += amp * _g(x, xk, 0.009)
    base = 0.10 + 0.55 * envf(x)
    y01 = np.clip(base + 0.80 * harm, 0.0, 0.95)
    return _to_dev(y01), _to_dev(base)


CURVE_FUNCS = {
    "eq": curve_eq,
    "compressor": curve_compressor,
    "reverb": curve_reverb,
    "delay": curve_delay,
    "saturation": curve_saturation,
    "limiter": curve_limiter,
    "deesser": curve_deesser,
    "imager": curve_imager,
    "flanger": curve_flanger,
    "chorus": curve_chorus,
    "phaser": curve_phaser,
    "universal": curve_universal,
}

# ---------------------------------------------------------------------------
# Captions: VTT parsing, fallback, wrapping, pagination
# ---------------------------------------------------------------------------

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")


def _ts_to_s(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_vtt(path: Optional[str]) -> List[Tuple[float, float, str]]:
    """Return [(start, end, text)] from a WebVTT file; [] if missing or unusable."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []

    cues: List[Tuple[float, float, str]] = []
    i = 0
    while i < len(lines):
        m = _CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start = _ts_to_s(*g[0:4])
        end = _ts_to_s(*g[4:8])
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines))
        text = re.sub(r"\s+", " ", text).strip()
        if text and end > start:
            cues.append((start, end, text))
    cues.sort(key=lambda c: c[0])
    return cues


def fallback_cues(text: str, duration: float) -> List[Tuple[float, float, str]]:
    """Split the script into sentences and spread them evenly (by length) over the audio."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    total = sum(len(p) for p in parts) or 1
    cues, t = [], 0.0
    for p in parts:
        span = duration * len(p) / total
        cues.append((t, t + span, p))
        t += span
    return cues


def merge_short_cues(cues, page_chars: int):
    """Word-level VTTs flicker; merge consecutive cues into phrase-sized captions."""
    merged = []
    for start, end, text in cues:
        if merged:
            ps, pe, pt = merged[-1]
            fits = len(pt) + 1 + len(text) <= page_chars
            ended = pt.rstrip().endswith((".", "!", "?"))
            if fits and not ended and start - pe < 0.6:
                merged[-1] = (ps, end, f"{pt} {text}")
                continue
        merged.append((start, end, text))
    return merged


def paginate(cues, chars_per_line: int, max_lines: int, duration: float):
    """Word-wrap each cue; split long cues into pages of <= max_lines, timed by length."""
    pages: List[Tuple[float, float, str]] = []
    for start, end, text in cues:
        start = max(0.0, min(start, duration))
        end = max(start + 0.05, min(end, duration))
        lines = textwrap.wrap(text, width=chars_per_line, break_long_words=True) or [text]
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        total = sum(sum(len(l) for l in c) for c in chunks) or 1
        t = start
        for c in chunks:
            span = (end - start) * sum(len(l) for l in c) / total
            pages.append((t, t + span, "\n".join(c)))
            t += span
    # Hold a caption through short gaps so it doesn't flash off between cues.
    for i in range(len(pages) - 1):
        s, e, tx = pages[i]
        if 0 < pages[i + 1][0] - e < 0.5:
            pages[i] = (s, pages[i + 1][0], tx)
    if pages:
        s, e, tx = pages[-1]
        pages[-1] = (s, max(e, duration), tx)  # last caption stays to the end
    return pages


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------

CHASSIS = "#141619"
SCREEN = "#0B0D10"
BEZEL_LIGHT = "#9AA3AF"
BEZEL_DARK = "#4B525C"
TEXT_DIM = "#8B93A0"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class MixingGraph(Scene):
    def construct(self):
        # ---- configuration ------------------------------------------------
        scene_text = os.environ.get("SCENE_TEXT", "")
        vtt_path = os.environ.get("VTT_PATH", "")
        mode = os.environ.get("VISUAL_MODE", "universal").strip().lower()
        if mode not in PRESETS:
            mode = "universal"
        video_type = os.environ.get("VIDEO_TYPE", "short").strip().lower()
        is_short = video_type != "long"
        x_pct = _clamp(_env_float("TEXT_X_PCT", 0.45 if is_short else 0.50), 0.0, 1.0)
        y_pct = _clamp(_env_float("TEXT_Y_PCT", 0.87 if is_short else 0.89), 0.0, 1.0)
        duration = _env_float("AUDIO_DURATION", 3.5)
        if duration <= 0:
            duration = 3.5
        power = max(0.1, _env_float("CURVE_POWER", 2.0))
        gain = _env_float("CURVE_GAIN", 0.85)

        preset = PRESETS[mode]
        accent = preset["color"]
        title = os.environ.get("CUSTOM_TITLE") or preset["title"]
        knob_labels = _env_labels("CUSTOM_KNOBS", 3) or preset["knobs"]
        x_labels = _env_labels("CUSTOM_X_LABELS", 6) or preset["x"]
        y_labels = _env_labels("CUSTOM_Y_LABELS", 5) or preset["y"]
        curve_fn: Callable = CURVE_FUNCS[mode]

        W, H = config.frame_width, config.frame_height
        self.camera.background_color = "#08090B"
        tracker = ValueTracker(0.0)
        self.add(tracker)

        # ---- layout -------------------------------------------------------
        if is_short:
            cx0, cx1 = -W / 2 + 0.16, W / 2 - 0.16
            cy_top, cy_bot = H / 2 - 0.16, -2.05
            header_h = 0.80
            y_gutter = 0.55
            screen_l = cx0 + 0.18 + y_gutter
            screen_r = cx1 - 0.20
            screen_t = cy_top - header_h - 0.05
            screen_b = screen_t - 2.85
            tick_fs, label_fs, title_fs = 11, 14, 24
            cap_fs, cap_lines, cap_width = 20, 3, W * 0.92
            knob_r = 0.40
        else:
            cx0, cx1 = -W / 2 + 0.30, W / 2 - 0.30
            cy_top, cy_bot = H / 2 - 0.28, -2.38
            header_h = 0.85
            y_gutter = 0.75
            screen_l = cx0 + 0.25 + y_gutter
            screen_r = 2.70
            screen_t = cy_top - header_h - 0.05
            screen_b = cy_bot + 0.80
            tick_fs, label_fs, title_fs = 15, 17, 30
            cap_fs, cap_lines, cap_width = 26, 2, W * 0.72
            knob_r = 0.50
        sw, sh = screen_r - screen_l, screen_t - screen_b

        # ---- chassis: brushed charcoal, metallic bezel, screws ------------
        chassis = RoundedRectangle(
            corner_radius=0.16, width=cx1 - cx0, height=cy_top - cy_bot,
            fill_color=CHASSIS, fill_opacity=1, stroke_width=0,
        ).move_to([(cx0 + cx1) / 2, (cy_top + cy_bot) / 2, 0])
        brushed = VGroup(*[
            Line([cx0 + 0.1, yy, 0], [cx1 - 0.1, yy, 0],
                 stroke_width=0.6, stroke_color="#FFFFFF",
                 stroke_opacity=0.035 if int(yy * 100) % 2 else 0.018)
            for yy in np.arange(cy_bot + 0.1, cy_top - 0.05, 0.09)
        ])
        bezel_outer = RoundedRectangle(
            corner_radius=0.16, width=cx1 - cx0, height=cy_top - cy_bot,
            fill_opacity=0,
        ).move_to(chassis.get_center())
        bezel_outer.set_stroke(color=[BEZEL_LIGHT, BEZEL_DARK, BEZEL_LIGHT], width=5)
        bezel_inner = RoundedRectangle(
            corner_radius=0.12, width=cx1 - cx0 - 0.10, height=cy_top - cy_bot - 0.10,
            fill_opacity=0, stroke_color="#2A2E35", stroke_width=1.5,
        ).move_to(chassis.get_center())
        screws = VGroup()
        for sx in (cx0 + 0.20, cx1 - 0.20):
            for sy in (cy_top - 0.20, cy_bot + 0.20):
                screw = Circle(radius=0.07, fill_color="#3A3F48", fill_opacity=1,
                               stroke_color="#7B8491", stroke_width=1.2).move_to([sx, sy, 0])
                slot = Line([sx - 0.045, sy - 0.045, 0], [sx + 0.045, sy + 0.045, 0],
                            stroke_color="#14161A", stroke_width=1.5)
                screws.add(screw, slot)
        self.add(chassis, brushed, bezel_inner, bezel_outer, screws)

        # ---- header: title + glowing LED ----------------------------------
        header_cy = cy_top - header_h / 2 - 0.02
        led_x = cx1 - 0.55
        title_txt = Text(title, font_size=title_fs, weight=BOLD, color="#E6E9EE")
        max_title_w = (led_x - 0.3) - (cx0 + 0.55)
        if title_txt.width > max_title_w:
            title_txt.scale_to_fit_width(max_title_w)
        title_txt.move_to([cx0 + 0.45 + title_txt.width / 2, header_cy, 0])
        divider = Line([cx0 + 0.3, cy_top - header_h, 0], [cx1 - 0.3, cy_top - header_h, 0],
                       stroke_color="#2E343D", stroke_width=1.5)
        led_glow2 = Circle(radius=0.20, fill_color=accent, fill_opacity=0.10, stroke_width=0).move_to([led_x, header_cy, 0])
        led_glow1 = Circle(radius=0.13, fill_color=accent, fill_opacity=0.25, stroke_width=0).move_to([led_x, header_cy, 0])
        led_core = Circle(radius=0.07, fill_color=accent, fill_opacity=1,
                          stroke_color="#FFFFFF", stroke_width=0.8, stroke_opacity=0.5).move_to([led_x, header_cy, 0])

        def led_updater(_):
            t = tracker.get_value()
            pulse = 0.5 + 0.5 * math.sin(2.4 * t)
            led_glow1.set_fill(opacity=0.15 + 0.25 * pulse)
            led_glow2.set_fill(opacity=0.05 + 0.12 * pulse)

        led_core.add_updater(led_updater)
        self.add(title_txt, divider, led_glow2, led_glow1, led_core)

        # ---- screen with grid, ticks and labels ---------------------------
        screen = RoundedRectangle(
            corner_radius=0.06, width=sw, height=sh,
            fill_color=SCREEN, fill_opacity=1, stroke_color="#2E343D", stroke_width=2,
        ).move_to([(screen_l + screen_r) / 2, (screen_t + screen_b) / 2, 0])
        self.add(screen)

        def sx(xn):
            return screen_l + xn * sw

        def sy(yn):
            return screen_b + yn * sh

        y_ticks = [(j + 0.5) / 5.0 for j in range(5)]
        grid = VGroup()
        for i in range(6):  # minor verticals halfway between ticks
            if i < 5:
                mid = (TICKS_X[i] + TICKS_X[i + 1]) / 2
                grid.add(Line([sx(mid), screen_b, 0], [sx(mid), screen_t, 0],
                              stroke_color="#FFFFFF", stroke_width=0.6, stroke_opacity=0.03))
        for xn in TICKS_X:
            grid.add(Line([sx(xn), screen_b, 0], [sx(xn), screen_t, 0],
                          stroke_color="#FFFFFF", stroke_width=1, stroke_opacity=0.08))
        for yn in y_ticks:
            grid.add(Line([screen_l, sy(yn), 0], [screen_r, sy(yn), 0],
                          stroke_color="#FFFFFF", stroke_width=1, stroke_opacity=0.08))
        self.add(grid)

        labels = VGroup()
        slot_w = sw / 6.0
        for xn, lab in zip(TICKS_X, x_labels):
            t = Text(lab, font_size=tick_fs, color=TEXT_DIM)
            if t.width > slot_w * 0.92:
                t.scale_to_fit_width(slot_w * 0.92)
            t.move_to([sx(xn), screen_b - 0.17 - t.height / 2 + 0.05, 0])
            labels.add(t)
            labels.add(Line([sx(xn), screen_b, 0], [sx(xn), screen_b - 0.05, 0],
                            stroke_color=TEXT_DIM, stroke_width=1.2))
        for yn, lab in zip(y_ticks, y_labels):
            t = Text(lab, font_size=tick_fs, color=TEXT_DIM)
            if t.width > y_gutter * 0.9:
                t.scale_to_fit_width(y_gutter * 0.9)
            t.move_to([screen_l - 0.10 - t.width / 2, sy(yn), 0])
            labels.add(t)
            labels.add(Line([screen_l, sy(yn), 0], [screen_l - 0.05, sy(yn), 0],
                            stroke_color=TEXT_DIM, stroke_width=1.2))
        self.add(labels)

        # ---- response curve ------------------------------------------------
        n_pts = 150 if is_short else 240
        xs = np.linspace(0.0, 1.0, n_pts)
        exponent = 2.0 / power

        def to_y01(dev):
            dev = np.clip(dev, -1.0, 1.0)
            shaped = np.sign(dev) * np.abs(dev) ** exponent
            return np.clip(0.5 + 0.5 * gain * shaped, 0.0, 1.0)

        def pts_for(dev):
            y01 = to_y01(dev)
            return np.column_stack([sx(xs), sy(y01), np.zeros(n_pts)])

        _, ghost0 = curve_fn(xs, 0.0)
        has_ghost = ghost0 is not None

        ghost_line = VMobject(stroke_color=accent, stroke_width=1.6, stroke_opacity=0.35, fill_opacity=0)
        glow_line = VMobject(stroke_color=accent, stroke_width=9, stroke_opacity=0.16, fill_opacity=0)
        main_line = VMobject(stroke_color=accent, stroke_width=3.2, stroke_opacity=1.0, fill_opacity=0)

        def curve_updater(_):
            t = tracker.get_value()
            primary, ghost = curve_fn(xs, t)
            p = pts_for(primary)
            main_line.set_points_as_corners(p)
            glow_line.set_points_as_corners(p)
            if has_ghost and ghost is not None:
                ghost_line.set_points_as_corners(pts_for(ghost))

        main_line.add_updater(curve_updater)
        curve_updater(None)
        self.add(ghost_line, glow_line, main_line)

        # Limiter: static hard ceiling line at the -1 dB level.
        if mode == "limiter":
            ceil_y = float(sy(float(to_y01(np.array([_to_dev(LIMITER_CEILING)]))[0])))
            self.add(DashedLine([screen_l, ceil_y, 0], [screen_r, ceil_y, 0],
                                stroke_color=accent, stroke_width=1.6, stroke_opacity=0.7,
                                dash_length=0.08))

        # Playhead (sweeps the screen over the whole clip) + live marker dot.
        playhead = Line([screen_l, screen_b, 0], [screen_l, screen_t, 0],
                        stroke_color="#FFFFFF", stroke_width=1.4, stroke_opacity=0.28)
        marker_glow = Dot(radius=0.11, color=accent, fill_opacity=0.25)
        marker = Dot(radius=0.055, color="#FFFFFF", fill_opacity=1)

        def playhead_updater(_):
            t = tracker.get_value()
            xn = _clamp(t / duration, 0.0, 1.0)
            x = sx(xn)
            playhead.put_start_and_end_on([x, screen_b, 0], [x, screen_t, 0])
            primary, _ghost = curve_fn(np.array([xn]), t)
            y = float(sy(float(to_y01(primary)[0])))
            marker.move_to([x, y, 0])
            marker_glow.move_to([x, y, 0])

        playhead.add_updater(playhead_updater)
        playhead_updater(None)
        self.add(playhead, marker_glow, marker)

        # Gain-reduction meter (compressor / limiter): bar hangs from the top-right.
        if mode in ("compressor", "limiter"):
            meter_w = 0.13 if is_short else 0.18
            meter_x = screen_r - 0.12 - meter_w / 2
            meter_top, meter_h = screen_t - 0.10, sh - 0.20
            self.add(RoundedRectangle(
                corner_radius=0.03, width=meter_w, height=meter_h,
                fill_color="#15181D", fill_opacity=0.85,
                stroke_color="#3A404A", stroke_width=1,
            ).move_to([meter_x, meter_top - meter_h / 2, 0]))
            gr_bar = VMobject()
            self.add(gr_bar)

            def meter_updater(m):
                t = tracker.get_value()
                if mode == "compressor":
                    level = -14.0 + 14.0 * math.sin(1.3 * t) ** 2 - 4.0
                    gr = float(_comp_gr(np.array([level]), t)[0]) / 12.0
                else:
                    gr = 0.55 * max(0.0, math.sin(2.1 * t)) ** 2 + 0.25 * max(0.0, math.sin(5.3 * t + 1.0)) ** 2
                gr = _clamp(gr, 0.0, 1.0)
                h = max(0.004, meter_h * gr)
                bar = RoundedRectangle(
                    corner_radius=min(0.03, h / 2), width=meter_w - 0.04, height=h,
                    fill_color=accent, fill_opacity=0.95, stroke_width=0,
                ).move_to([meter_x, meter_top - h / 2, 0])
                m.become(bar)

            gr_bar.add_updater(meter_updater)
            meter_updater(gr_bar)

        # ---- rotary knobs ---------------------------------------------------
        if is_short:
            knob_zone_top = screen_b - 0.50
            knob_zone_bot = cy_bot + 0.12
            block_h = 2 * knob_r + 0.10 + 0.25
            knob_cy = (knob_zone_top + knob_zone_bot) / 2 + block_h / 2 - knob_r
            spacing = (cx1 - cx0 - 0.4) / 3.0
            knob_centers = [
                [(cx0 + cx1) / 2 + (i - 1) * spacing, knob_cy, 0] for i in range(3)
            ]
        else:
            col_x = (screen_r + (cx1 - 0.25)) / 2 + 0.05
            top = screen_t - knob_r - 0.10
            last_cy = cy_bot + 0.22 + knob_r * 1.45 + 0.34
            step = (top - last_cy) / 2.0
            knob_centers = [[col_x, top - i * step, 0] for i in range(3)]

        def knob_angle(i, t):
            v = (0.5 + 0.22 * math.sin(0.55 * t * (1 + 0.17 * i) + 1.3 * i)
                 + 0.12 * (t / duration - 0.5) * (1 if i % 2 == 0 else -1))
            v = _clamp(v, 0.08, 0.92)
            return math.radians(225.0 - 270.0 * v)

        for i, (c, lab) in enumerate(zip(knob_centers, knob_labels)):
            cx_, cy_ = c[0], c[1]
            ticks = VGroup()
            for k in range(11):
                a = math.radians(225.0 - 27.0 * k)
                r0, r1 = knob_r * 1.20, knob_r * (1.36 if k in (0, 5, 10) else 1.30)
                ticks.add(Line(
                    [cx_ + r0 * math.cos(a), cy_ + r0 * math.sin(a), 0],
                    [cx_ + r1 * math.cos(a), cy_ + r1 * math.sin(a), 0],
                    stroke_color=accent if k in (0, 10) else "#4A515C", stroke_width=1.6,
                ))
            base = Circle(radius=knob_r * 1.10, fill_color="#0E1013", fill_opacity=1,
                          stroke_color="#2B3038", stroke_width=2).move_to(c)
            body = Circle(radius=knob_r, fill_color="#23272E", fill_opacity=1,
                          stroke_color="#6B7380", stroke_width=2.2).move_to(c)
            cap = Circle(radius=knob_r * 0.70, fill_color="#1A1D22", fill_opacity=1,
                         stroke_color="#3A404A", stroke_width=1.5).move_to(c)
            needle_glow = Line(c, c, stroke_color=accent, stroke_width=8, stroke_opacity=0.25)
            needle = Line(c, c, stroke_color=accent, stroke_width=3.6)
            hub = Dot(c, radius=knob_r * 0.10, color="#3A404A")

            def make_needle_updater(idx, cx__, cy__):
                def _upd(m):
                    a = knob_angle(idx, tracker.get_value())
                    d = np.array([math.cos(a), math.sin(a), 0.0])
                    ctr = np.array([cx__, cy__, 0.0])
                    m.put_start_and_end_on(ctr + d * knob_r * 0.14, ctr + d * knob_r * 0.92)
                return _upd

            upd = make_needle_updater(i, cx_, cy_)
            needle.add_updater(upd)
            needle_glow.add_updater(upd)
            upd(needle)
            upd(needle_glow)

            k_label = Text(lab, font_size=label_fs, color="#C9CED6")
            max_lab_w = knob_r * 3.0 if not is_short else (cx1 - cx0 - 0.4) / 3.0 * 0.92
            if k_label.width > max_lab_w:
                k_label.scale_to_fit_width(max_lab_w)
            k_label.move_to([cx_, cy_ - knob_r * 1.45 - k_label.height / 2 - 0.02, 0])
            self.add(ticks, base, body, cap, needle_glow, needle, hub, k_label)

        # ---- captions -------------------------------------------------------
        sample = Text("The quick brown fox jumps over the lazy dog", font_size=cap_fs)
        char_w = sample.width / 43.0
        pad_x, pad_y = 0.22, 0.14
        chars_per_line = max(10, int((cap_width - 2 * pad_x) / char_w))

        cues = parse_vtt(vtt_path)
        if not cues:
            cues = fallback_cues(scene_text, duration)
        cues = merge_short_cues(cues, chars_per_line * cap_lines)
        pages = paginate(cues, chars_per_line, cap_lines, duration)

        anchor_x = -W / 2 + x_pct * W
        anchor_y = H / 2 - y_pct * H

        def build_caption(text: str) -> VGroup:
            clean = text.replace("<", "").replace(">", "")
            lines = VGroup(*[Text(l, font_size=cap_fs, color="#FFFFFF") for l in clean.split("\n")])
            lines.arrange(DOWN, buff=0.07)
            if lines.width > cap_width - 2 * pad_x:
                lines.scale_to_fit_width(cap_width - 2 * pad_x)
            bw, bh = lines.width + 2 * pad_x, lines.height + 2 * pad_y
            box = RoundedRectangle(
                corner_radius=min(bh / 2, 0.30), width=bw, height=bh,
                fill_color="#05070A", fill_opacity=0.78,
                stroke_color=accent, stroke_width=1.2, stroke_opacity=0.55,
            )
            grp = VGroup(box, lines)
            cx_c = _clamp(anchor_x, -W / 2 + 0.06 + bw / 2, W / 2 - 0.06 - bw / 2)
            cy_c = _clamp(anchor_y, -H / 2 + 0.06 + bh / 2, H / 2 - 0.06 - bh / 2)
            grp.move_to([cx_c, cy_c, 0])
            return grp

        def blank_caption() -> VGroup:
            return VGroup(Dot(radius=0.001, fill_opacity=0, stroke_width=0).move_to([anchor_x, anchor_y, 0]))

        caption_cache = {}
        starts = [p[0] for p in pages]
        ends = [p[1] for p in pages]
        state = {"idx": -2}
        holder = blank_caption()

        def caption_updater(m):
            t = tracker.get_value()
            idx = bisect_right(starts, t) - 1
            if idx >= 0 and t >= ends[idx]:
                idx = -1
            if idx == state["idx"]:
                return
            state["idx"] = idx
            if idx < 0:
                m.become(blank_caption())
            else:
                if idx not in caption_cache:
                    caption_cache[idx] = build_caption(pages[idx][2])
                m.become(caption_cache[idx])

        holder.add_updater(caption_updater)
        caption_updater(holder)
        self.add(holder)

        # ---- run: animation length == audio length --------------------------
        self.play(tracker.animate.set_value(duration), run_time=duration, rate_func=linear)
