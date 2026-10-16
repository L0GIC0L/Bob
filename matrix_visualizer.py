#!/usr/bin/env python3
"""Matrix-style music visualizer for GTK4.

Listens to whatever the desktop is playing (the PulseAudio / PipeWire monitor
source, captured with `parec`), splits it into log-spaced frequency bands and
draws every band as a column of green code: the spectrum rises from the floor
while digital rain falls faster and brighter where the music is loud.

Controls:  F / F11 fullscreen    + / - glyph size    Esc / Q quit
"""

import argparse
import math
import subprocess
import sys
import threading
import time

import cairo
import gi
import numpy as np

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

# Audio analysis
RATE = 44100            # capture sample rate (Hz)
FFT_SIZE = 4096         # ~93 ms window, enough resolution for the bass end
CHUNK = 512             # samples per read from parec (~12 ms)
F_MIN, F_MAX = 30.0, 16000.0
TILT_DB = 4.5           # dB/octave boost so treble isn't buried under bass
RANGE_DB = 50.0         # dynamic range between an empty and a full column
CEILING_MIN_DB = -45.0  # auto-gain never amplifies past this (keeps hiss dark)
CEILING_FALL = 4.0      # auto-gain recovery after loud passages (dB/s)
RELEASE_TIME = 0.15     # column fall-back smoothing (s); rises are instant

# Look
TRAIL_TIME = 0.35       # rain trail fade time constant (s)
RAIN_GAIN = 0.75        # brightest rain relative to the spectrum columns
RAIN_SPEED = (8.0, 20.0)  # base fall speed range (cells/s)
FLICKER_RATE = 1.5      # glyph changes per cell per second
PEAK_HOLD = 0.4         # how long a peak marker hangs before dripping (s)
PEAK_FALL = 12.0        # peak marker fall speed (cells/s)
SHADES = 12             # number of green shades brightness is binned into
HEAD_RGB = (0.78, 1.0, 0.84)

KATAKANA = "".join(chr(c) for c in range(0xFF66, 0xFF9E))  # half-width ｦ..ﾝ
DIGITS = "0123456789"
ASCII_GLYPHS = DIGITS + "ABCDEFGHIJKLMNOPQRSTUVWXYZ:.=*+-<>|"


def _shade(v):
    """Dark green -> matrix green -> pale green as v goes 0 -> 1."""
    return (0.55 * v ** 4, 0.08 + 0.92 * v, 0.25 * v ** 2)


PALETTE = [_shade((i + 0.5) / SHADES) for i in range(SHADES)]


class MonitorCapture:
    """Mono samples from a PulseAudio/PipeWire source, read through `parec`."""

    def __init__(self, device):
        self.alive = True
        self._buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            ["parec", "--raw", f"--device={device}", "--format=s16le",
             f"--rate={RATE}", "--channels=1", "--latency-msec=20"],
            stdout=subprocess.PIPE)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        nbytes = CHUNK * 2
        while True:
            data = self._proc.stdout.read(nbytes)
            if len(data) < nbytes:
                break
            chunk = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            with self._lock:
                self._buf[:-CHUNK] = self._buf[CHUNK:]
                self._buf[-CHUNK:] = chunk
        self.alive = False

    def samples(self, dt):
        with self._lock:
            return self._buf.copy()

    def close(self):
        if self._proc.poll() is None:
            self._proc.terminate()


class DemoSignal:
    """A synthetic 120 BPM groove, used when there is nothing to capture."""

    alive = True

    def __init__(self):
        self._t = 0.0
        self._rng = np.random.default_rng()

    def samples(self, dt):
        self._t += dt
        t = self._t - FFT_SIZE / RATE + np.arange(FFT_SIZE) / RATE
        beat = (t * 2.0) % 1.0
        eighth = (t * 4.0) % 1.0
        root = np.array([55.0, 43.65, 65.41, 49.0])[(t / 2.0).astype(int) % 4]
        step = (t * 4.0).astype(int) % 8
        harmonic = np.array([4, 5, 6, 8, 12, 8, 6, 5])[step]

        kick = np.sin(2 * np.pi * (50 * beat + 3.6 * (1 - np.exp(-25 * beat))))
        kick *= 0.6 * np.exp(-8 * beat)
        bass = 0.5 * np.sin(2 * np.pi * root * t) * np.exp(-3 * eighth)
        arp = 0.25 * np.sin(2 * np.pi * root * harmonic * t) * np.exp(-5 * eighth)
        hats = np.diff(self._rng.standard_normal(FFT_SIZE + 1)) * 0.1
        hats *= np.exp(-40 * eighth)
        return (kick + bass + arp + hats).astype(np.float32)

    def close(self):
        pass


class Spectrum:
    """Turns raw samples into `n_bands` log-spaced levels in the 0..1 range."""

    def __init__(self, n_bands):
        self.n_bands = n_bands
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        self.freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / RATE)
        edges = np.geomspace(F_MIN, F_MAX, n_bands + 1)
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.lo = np.searchsorted(self.freqs, edges[:-1])
        self.hi = np.maximum(np.searchsorted(self.freqs, edges[1:]), self.lo + 1)
        # Bass bands narrower than one FFT bin are interpolated instead,
        # otherwise neighbouring columns would show identical blocks.
        self.narrow = (self.hi - self.lo) <= 1
        self.tilt = TILT_DB * np.log2(self.centers / 1000.0)
        self.ceiling = CEILING_MIN_DB
        self.levels = np.zeros(n_bands)

    def update(self, samples, dt):
        # Scaled so a full-scale sine reads 0 dB through the Hann window.
        mag = np.abs(np.fft.rfft(samples * self.window)) / (FFT_SIZE / 4)
        power = mag * mag
        csum = np.concatenate(([0.0], np.cumsum(power)))
        band = (csum[self.hi] - csum[self.lo]) / (self.hi - self.lo)
        band = np.where(self.narrow, np.interp(self.centers, self.freqs, power), band)
        db = 10.0 * np.log10(band + 1e-12) + self.tilt

        # Simple auto-gain: the loudest band sets the top of the screen.
        self.ceiling = max(db.max(), self.ceiling - CEILING_FALL * dt, CEILING_MIN_DB)
        top = self.ceiling + 3.0
        target = np.clip((db - (top - RANGE_DB)) / RANGE_DB, 0.0, 1.0)

        release = 1.0 - math.exp(-dt / RELEASE_TIME)
        self.levels = np.where(target > self.levels, target,
                               self.levels + (target - self.levels) * release)
        return self.levels


class MatrixRain:
    """A grid of glyph cells: spectrum columns rising, digital rain falling."""

    def __init__(self, family, glyph_chars, font_size):
        self.family = family
        self.glyph_chars = glyph_chars
        self.font_size = font_size
        self.face = cairo.ToyFontFace(family)
        self.rng = np.random.default_rng()
        self.cols = self.rows = 0
        self.size = None

        ctx = cairo.Context(cairo.ImageSurface(cairo.FORMAT_A8, 1, 1))
        ctx.set_font_face(self.face)
        ctx.set_font_size(font_size)
        font = ctx.get_scaled_font()
        ids, advances = [], []
        for ch in glyph_chars:
            glyph = font.text_to_glyphs(0, 0, ch, False)[0]
            ids.append(glyph.index)
            advances.append(font.glyph_extents([glyph]).x_advance)
        ascent, descent = font.extents()[:2]
        self.cell_w = math.ceil(max(advances) * 1.25)
        self.cell_h = math.ceil(font_size * 1.15)
        self.glyph_ids = np.array(ids)
        self.glyph_dx = (self.cell_w - np.array(advances)) / 2.0
        self.baseline = (self.cell_h + ascent - descent) / 2.0

    def resize(self, width, height):
        if (width, height) == self.size:
            return
        self.size = (width, height)
        cols = max(1, width // self.cell_w)
        rows = max(1, math.ceil(height / self.cell_h))
        # Centre horizontally; pin the bottom row to the bottom edge.
        self.x = (width - cols * self.cell_w) / 2.0 + np.arange(cols) * self.cell_w
        self.y = (height - rows * self.cell_h) + np.arange(rows) * self.cell_h + self.baseline
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        rng = self.rng
        self.height_of_row = (rows - 1 - np.arange(rows))[:, None]
        self.trail = np.zeros((rows, cols))
        self.glyph = rng.integers(0, len(self.glyph_ids), (rows, cols))
        self.heads = rng.uniform(-rows, rows, cols)
        self.speed = rng.uniform(*RAIN_SPEED, cols)
        self.levels = np.zeros(cols)
        self.peak = np.zeros(cols)
        self.hold = np.zeros(cols)

    def step(self, levels, dt):
        rows, rng, n_glyphs = self.rows, self.rng, len(self.glyph_ids)
        self.levels = levels
        self.trail *= math.exp(-dt / TRAIL_TIME)

        # Rain: loud bands fall faster, so they also come around again sooner.
        new = self.heads + self.speed * (0.4 + 1.6 * levels) * dt
        first = np.maximum(np.floor(self.heads).astype(int) + 1, 0)
        last = np.minimum(np.floor(new).astype(int), rows - 1)
        for c in np.nonzero(last >= first)[0]:
            a, b = first[c], last[c] + 1
            self.trail[a:b, c] = 1.0
            self.glyph[a:b, c] = rng.integers(0, n_glyphs, b - a)
        gone = new >= rows
        new[gone] = -rng.uniform(0, rows, np.count_nonzero(gone))  # wait off-screen
        self.speed[gone] = rng.uniform(*RAIN_SPEED, np.count_nonzero(gone))
        self.heads = new

        # Peak markers hang above each column, then drip down leaving a trail.
        height = levels * rows
        rising = height >= self.peak
        self.peak = np.where(rising, height, self.peak)
        self.hold = np.where(rising, PEAK_HOLD, self.hold - dt)
        falling = ~rising & (self.hold <= 0)
        self.peak[falling] = np.maximum(self.peak[falling] - PEAK_FALL * dt, 0.0)
        row = rows - 1 - np.floor(self.peak).astype(int)
        drip = falling & (row >= 0) & (row < rows)
        self.trail[row[drip], np.nonzero(drip)[0]] = 1.0

        flip = rng.random((rows, self.cols)) < FLICKER_RATE * dt
        self.glyph[flip] = rng.integers(0, n_glyphs, np.count_nonzero(flip))

    def render(self, ctx):
        rows = self.rows
        height = self.levels * rows
        lit = self.height_of_row < height
        # Columns glow at the top and fade toward the floor.
        frac = (self.height_of_row + 1) / np.maximum(height, 1)
        column = np.where(lit, 0.18 + 0.82 * frac * frac, 0.0)
        rain = self.trail * (RAIN_GAIN * (0.45 + 0.55 * self.levels))
        shade = np.minimum((np.maximum(rain, column) * SHADES).astype(int), SHADES - 1)

        # Bright "head" glyphs: column tops, falling drops and peak markers.
        head = lit & (self.height_of_row + 1 >= height)
        for row, show in ((np.floor(self.heads).astype(int), True),
                          (rows - 1 - np.floor(self.peak).astype(int), self.peak >= 1)):
            show = show & (row >= 0) & (row < rows)
            head[row[show], np.nonzero(show)[0]] = True
        shade[head] = SHADES

        ctx.set_font_face(self.face)
        ctx.set_font_size(self.font_size)
        for s in range(1, SHADES + 1):
            r, c = np.nonzero(shade == s)
            if r.size == 0:
                continue
            g = self.glyph[r, c]
            xs = self.x[c] + self.glyph_dx[g]
            ctx.set_source_rgb(*(HEAD_RGB if s == SHADES else PALETTE[s]))
            ctx.show_glyphs(list(zip(self.glyph_ids[g].tolist(), xs.tolist(),
                                     self.y[r].tolist())))


class MatrixView(Gtk.DrawingArea):
    def __init__(self, source, family, glyph_chars, font_size):
        super().__init__(hexpand=True, vexpand=True)
        self.source = source
        self.rain = MatrixRain(family, glyph_chars, font_size)
        self.spectrum = None
        self._last_frame = None
        self._message, self._message_until = "", 0.0
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def notify_user(self, message, seconds=5.0):
        print(message, file=sys.stderr)
        self._message, self._message_until = message, time.monotonic() + seconds

    def set_font_size(self, size):
        size = min(max(size, 8), 48)
        self.rain = MatrixRain(self.rain.family, self.rain.glyph_chars, size)
        self.spectrum = None  # nothing to draw until the next tick builds the grid
        self.notify_user(f"glyph size {size}px", 1.5)

    def _tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = 1 / 60 if self._last_frame is None else min(now - self._last_frame, 0.1)
        self._last_frame = now
        if not self.source.alive:
            self.source = DemoSignal()
            self.notify_user("audio capture stopped - showing the demo signal")

        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:  # not laid out yet
            return GLib.SOURCE_CONTINUE
        self.rain.resize(width, height)
        if self.spectrum is None or self.spectrum.n_bands != self.rain.cols:
            self.spectrum = Spectrum(self.rain.cols)
        self.rain.step(self.spectrum.update(self.source.samples(dt), dt), dt)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw(self, area, ctx, width, height):
        ctx.set_source_rgb(0, 0, 0)
        ctx.paint()
        if self.spectrum is not None:
            self.rain.render(ctx)

        remaining = self._message_until - time.monotonic()
        if remaining > 0:
            alpha = min(1.0, remaining)
            ctx.select_font_face("Monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(14)
            text_w = ctx.text_extents(self._message).x_advance
            ctx.set_source_rgba(0, 0, 0, 0.8 * alpha)
            ctx.rectangle(8, 8, text_w + 16, 26)
            ctx.fill()
            ctx.set_source_rgba(*HEAD_RGB, alpha)
            ctx.move_to(16, 26)
            ctx.show_text(self._message)


class MatrixWindow(Gtk.ApplicationWindow):
    def __init__(self, app, view, fullscreen):
        super().__init__(application=app, title="Matrix Visualizer")
        self.set_default_size(1280, 720)
        self.view = view
        self.set_child(view)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        if fullscreen:
            self.fullscreen()

    def _on_key(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            self.unfullscreen() if self.is_fullscreen() else self.fullscreen()
        elif keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
        elif keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q):
            self.close()
        elif keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.view.set_font_size(self.view.rain.font_size + 2)
        elif keyval in (Gdk.KEY_minus, Gdk.KEY_underscore, Gdk.KEY_KP_Subtract):
            self.view.set_font_size(self.view.rain.font_size - 2)
        else:
            return False
        return True


def pick_font():
    """Find a font with half-width katakana, else fall back to ASCII glyphs."""
    context = PangoCairo.FontMap.get_default().create_context()
    fontset = context.load_fontset(Pango.FontDescription.from_string("Monospace 16"),
                                   Pango.Language.get_default())
    font = fontset.get_font(ord(KATAKANA[0]))  # whichever font fontconfig falls back to
    if all(font.has_char(ch) for ch in KATAKANA + DIGITS):
        return font.describe().get_family(), KATAKANA + DIGITS
    print("no font with katakana found (install a CJK font such as Noto Sans CJK);"
          " using ASCII glyphs", file=sys.stderr)
    return "Monospace", ASCII_GLYPHS


def open_source(args):
    if args.demo:
        return DemoSignal(), "demo mode - showing a synthetic signal"
    try:
        return MonitorCapture(args.device), None
    except FileNotFoundError:
        return DemoSignal(), "parec not found (install pulseaudio-utils) - showing the demo signal"


def main():
    parser = argparse.ArgumentParser(description="Matrix-style music visualizer (GTK4).")
    parser.add_argument("--device", default="@DEFAULT_MONITOR@",
                        help="PulseAudio/PipeWire source to capture; the default is "
                             "whatever is playing (run `pactl list short sources` to see others)")
    parser.add_argument("--demo", action="store_true",
                        help="ignore audio and visualize a built-in synthetic beat")
    parser.add_argument("--font-size", type=int, default=16, choices=range(8, 49),
                        metavar="8-48", help="glyph size in pixels (default: 16)")
    parser.add_argument("--fullscreen", action="store_true", help="start fullscreen")
    args = parser.parse_args()

    family, glyph_chars = pick_font()
    source, message = open_source(args)

    def on_activate(app):
        if app.get_active_window():
            app.get_active_window().present()
            return
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        view = MatrixView(source, family, glyph_chars, args.font_size)
        if message:
            view.notify_user(message)
        MatrixWindow(app, view, args.fullscreen).present()

    app = Gtk.Application(application_id="io.github.l0gic0l.MatrixVisualizer")
    app.connect("activate", on_activate)
    try:
        return app.run([sys.argv[0]])
    finally:
        source.close()


if __name__ == "__main__":
    sys.exit(main())
