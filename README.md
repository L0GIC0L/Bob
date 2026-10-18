# Matrix Visualizer

A GTK4 music visualizer in the style of the Matrix "digital rain".

It listens to whatever your desktop is playing, splits the sound into
frequency bands (bass on the left, treble on the right), and draws each band
as a column of green katakana:

- **Spectrum columns** rise from the floor. They glow at the top and fade
  toward the bottom, with a white glyph on each column's tip.
- **Peak markers** hang above each column for a moment, then drip down and
  leave a trail.
- **Digital rain** falls in every column. Loud bands make it fall faster and
  brighter, so the rain gets heavier when the music does.

## Install

You need Python 3, GTK 4 with its Python bindings, NumPy, `parec` (for
capturing audio) and a font that has Japanese katakana.

Ubuntu / Debian:

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 python3-numpy \
                 pulseaudio-utils fonts-noto-cjk
```

Fedora:

```sh
sudo dnf install python3-gobject gtk4 python3-numpy pulseaudio-utils \
                 google-noto-sans-cjk-fonts
```

Arch:

```sh
sudo pacman -S python-gobject python-cairo gtk4 python-numpy libpulse noto-fonts-cjk
```

`parec` works with both PulseAudio and PipeWire (through `pipewire-pulse`,
which current Ubuntu and Fedora desktops run by default).

## Run

```sh
./matrix_visualizer.py
```

Play some music and it reacts. Options:

| Option | What it does |
| --- | --- |
| `--fullscreen` | Start fullscreen |
| `--font-size N` | Glyph size in pixels, 8–48 (default 16) |
| `--device NAME` | Capture a different source, e.g. a microphone. List them with `pactl list short sources`. The default, `@DEFAULT_MONITOR@`, is whatever your speakers are playing. |
| `--demo` | Ignore audio and show a built-in synthetic beat |

Keys:

| Key | Action |
| --- | --- |
| `F` / `F11` | Toggle fullscreen |
| `+` / `-` | Bigger / smaller glyphs |
| `Esc` | Leave fullscreen, or quit |
| `Q` | Quit |

## Troubleshooting

- **Nothing moves / "showing the demo signal" message:** audio capture failed.
  The reason is printed in the terminal. Check that `parec` is installed and
  that `pactl info` shows a sound server.
- **Columns don't react to music:** check that the right output is the
  default (`pactl get-default-sink`), or pass a specific source with
  `--device` (names ending in `.monitor` capture an output).
- **Boxes or Latin letters instead of katakana:** no font with katakana was
  found, so it fell back to ASCII. Install the CJK font package listed above.
- **Stutters on a large or 4K screen:** press `+` a few times. Bigger glyphs
  mean fewer of them to draw each frame.

## How it works

`parec` streams the desktop audio as 16-bit samples into a background
thread. Every frame, the last 4096 samples (about 93 ms) go through a
Hann-windowed FFT. The FFT bins are grouped into log-spaced bands, one per
column, from 30 Hz to 16 kHz. A 4.5 dB/octave tilt keeps the treble visible
next to the bass, and a simple auto-gain fills the screen at any volume.
Drawing is plain Cairo inside a `Gtk.DrawingArea`, driven by the GTK frame
clock. Glyphs are grouped by shade and drawn with one `show_glyphs` call
per group, which keeps Python fast enough for 60 fps.
