"""The picture of a player's Legend day so far, at the bottom of an attack
alert and in /stats, light unless the Clasher chose dark on the website's
Discord alerts page. It is drawn as a small dashboard built from square
pixels: the trophies through the day as a stepped line over a pixel-shaded
area, a dial of the day's 16 battles, and a row per battle whose pixel bar is
as long as its trophies and as dense as its destruction.

Pillow draws with one font at a time, so text is split into the characters a
reader sees, each drawn with the first font that has it: Geist and Geist Mono
(copied into `fonts`), then Noto for other scripts and symbols, then Noto's
colour emoji. The container image carries Noto (see python/Containerfile); a
Noto file that is missing is skipped.
"""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import regex
from PIL import Image, ImageChops, ImageDraw, ImageFont

_FONTS = Path(__file__).parent / "fonts"
_NOTO = Path("/usr/share/fonts/truetype/noto")
_CJK = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
# Each font: its file, its place in a font collection, and the weight to use
# from a file that holds every weight.
_FACES: dict[str, tuple[Path, int, int | None]] = {
    "name": (_FONTS / "geist-latin-600-normal.woff2", 0, None),
    "body": (_FONTS / "geist-latin-500-normal.woff2", 0, None),
    "mono": (_FONTS / "geist-mono-latin-500-normal.woff2", 0, None),
    "noto": (_NOTO / "NotoSans-Bold.ttf", 0, None),
    "korean": (_CJK, 1, None),
    "chinese": (_CJK, 2, None),
    **{
        script: (_NOTO / f"NotoSans{script}-Bold.ttf", 0, None)
        for script in ("Khmer", "Arabic", "Thai", "Devanagari", "Hebrew", "Bengali",
                       "Tamil", "Myanmar", "Georgian", "Armenian", "Javanese", "Symbols")
    },
    "Tibetan": (_NOTO / "NotoSerifTibetan-Bold.ttf", 0, None),
    "Symbols2": (_NOTO / "NotoSansSymbols2-Regular.ttf", 0, None),
    "Math": (_NOTO / "NotoSansMath-Regular.ttf", 0, None),
}
_WEBSITE = ("name", "body", "mono")
_FALLBACK = tuple(name for name in _FACES if name not in _WEBSITE)
_EMOJI = _NOTO / "NotoColorEmoji.ttf"
_EMOJI_SIZE = 109  # the only size the colour emoji font draws at
# A character no font has, so a font draws its box for a missing one.
_MISSING = "\U0010fffd"
_INVISIBLE = frozenset("\u200d\ufe0f\ufe0e\u200b\u200c\u2060")
# The website's brand colours (website/app/theme.css).
_YELLOW, _ORANGE, _BRAND_NIGHT = "#ffd447", "#ff8a1f", "#1a1206"
_LIGHT = {
    "paper": "#f4f4f5", "card": "#ffffff", "line": "#e3e3e6", "ink": "#0b0b0d",
    "muted": "#5b5b65", "faint": "#a1a1aa", "off": "#e4e4e7", "star": "#e8a200",
    "positive": "#15803d", "negative": "#dc2626", "new": "#fff4e8",
    "fill": ("#ff8a1f", "#c2410c"), "chart": "#ea6c00",
}
_DARK = {
    "paper": "#050506", "card": "#0f0f11", "line": "#232327", "ink": "#ece8e1",
    "muted": "#8d8a84", "faint": "#5c5a56", "off": "#26262a", "star": "#ffc53d",
    "positive": "#4ade80", "negative": "#ff6b5b", "new": "#ff8a1f17",
    "fill": (_YELLOW, _ORANGE), "chart": _ORANGE,
}
# Each square pixel lights where the shade wanted there is above its place in
# this 8 by 8 pattern, so a smooth shade becomes an even spread of pixels.
_PATTERN = [[0]]
for _ in range(3):
    _PATTERN = [[4 * _PATTERN[y % len(_PATTERN)][x % len(_PATTERN)] + (0, 2, 3, 1)[(y // len(_PATTERN)) * 2 + x // len(_PATTERN)]
                 for x in range(len(_PATTERN) * 2)] for y in range(len(_PATTERN) * 2)]
_PIXEL = 4  # a pixel's side, in picture coordinates
WIDTH, HEIGHT = 1200, 860
# Drawn at twice the size, then shrunk, for smooth edges.
_SCALE = 2
_MOST = 8  # the game's attacks, and defenses, a Legend day


@cache
def _face(name: str, size: int) -> ImageFont.FreeTypeFont | None:
    path, index, weight = _FACES[name]
    if not path.exists():
        return None
    font = ImageFont.truetype(path, size, index=index)
    if weight:
        font.set_variation_by_axes([weight])
    return font


@cache
def _emoji() -> ImageFont.FreeTypeFont | None:
    return ImageFont.truetype(_EMOJI, _EMOJI_SIZE) if _EMOJI.exists() else None


def _ink(font: ImageFont.FreeTypeFont, text: str) -> bytes:
    image = Image.new("RGBA", (160, 160))
    ImageDraw.Draw(image).text((4, 4), text, font=font, fill="white", embedded_color=True)
    return image.tobytes()


# Names bring any characters, so only the most recently checked are kept.
@lru_cache(maxsize=16384)
def _draws(name: str, text: str) -> bool:
    """Whether font `name` ("emoji" for colour emoji) draws `text` itself,
    not its box for a missing character."""
    if text.isspace():
        return True
    font = _emoji() if name == "emoji" else _face(name, 48)
    return font is not None and _ink(font, text) != _ink(font, _MISSING)


def _runs(text: str, style: str) -> list[tuple[str, str]]:
    """`text` as (font, text) runs: each character a reader sees in the first
    font that draws it, starting with `style`."""
    runs: list[tuple[str, str]] = []
    for cluster in regex.findall(r"\X", text):
        emoji = any(ord(char) >= 0x1F000 or char in "\u200d\ufe0f" for char in cluster)
        visible = "".join(char for char in cluster if char not in _INVISIBLE)
        if emoji and _draws("emoji", cluster):
            name = "emoji"
        else:
            found = next(
                (name for name in (style, *_FALLBACK)
                 if all(_draws(name, char) for char in visible)),
                None,
            )
            name = found or ("emoji" if _draws("emoji", cluster) else style)
            cluster = cluster if name == "emoji" else visible
        if runs and runs[-1][0] == name:
            runs[-1] = (name, runs[-1][1] + cluster)
        else:
            runs.append((name, cluster))
    return runs


def _run_width(name: str, text: str, size: int) -> float:
    if name == "emoji":
        return _emoji().getlength(text) * size * 1.05 / _EMOJI_SIZE  # type: ignore[union-attr]
    return _face(name, size).getlength(text)  # type: ignore[union-attr]


def _measure(text: str, size: int, style: str) -> float:
    return sum(_run_width(name, part, size) for name, part in _runs(text, style))


def _fit(text: str, size: int, width: float, style: str) -> str:
    """`text`, cut short with an ellipsis to fit `width`."""
    if _measure(text, size, style) <= width:
        return text
    clusters = regex.findall(r"\X", text)
    while clusters and _measure("".join(clusters) + "…", size, style) > width:
        clusters.pop()
    return "".join(clusters).rstrip() + "…"


class _Canvas:
    """The picture at twice its size, drawn in picture coordinates."""

    def __init__(self, colours: Mapping[str, Any]) -> None:
        self.colours = colours
        self.image = Image.new("RGBA", (WIDTH * _SCALE, HEIGHT * _SCALE), colours["paper"])

    @staticmethod
    def s(value: float) -> int:
        return round(value * _SCALE)

    def text(
        self, x: float, y: float, text: str, size: int, colour: str, style: str = "name",
        anchor: str = "left", width: float | None = None, spaced: bool = False,
    ) -> float:
        """Draw `text` with its baseline at `y`, starting, centred or ending
        at `x` by `anchor`, cut short to `width`, its letters `spaced` apart
        as small capitals are; returns its width."""
        size = self.s(size)
        if width is not None:
            text = _fit(text, size, self.s(width), style)
        runs = _runs(text, style)
        if spaced:
            runs = [(name, char) for name, part in runs for char in regex.findall(r"\X", part)]
        gap = size * 0.14 if spaced else 0
        total = sum(_run_width(name, part, size) + gap for name, part in runs) - gap
        left = self.s(x) - {"left": 0, "middle": total / 2, "right": total}[anchor]
        draw = ImageDraw.Draw(self.image)
        for name, part in runs:
            if name == "emoji":
                font = _emoji()
                tile = Image.new("RGBA", (int(font.getlength(part)) + 8, int(_EMOJI_SIZE * 1.3)))  # type: ignore[union-attr]
                ImageDraw.Draw(tile).text((0, 0), part, font=font, embedded_color=True)
                scale = size * 1.05 / _EMOJI_SIZE
                tile = tile.resize((max(1, int(tile.width * scale)), max(1, int(tile.height * scale))),
                                   Image.Resampling.LANCZOS)
                self.image.alpha_composite(tile, (int(left), int(self.s(y) - size * 0.98)))
            else:
                draw.text((left, self.s(y)), part, font=_face(name, size), fill=colour,
                          anchor="ls")
            left += _run_width(name, part, size) + gap
        return total / _SCALE

    def block(self, box: Sequence[float], fill: str | None, outline: str | None = None,
              width: int = 2, radius: int = 12) -> None:
        x0, y0, x1, y1 = (self.s(value) for value in box)
        layer = Image.new("RGBA", (x1 - x0 + 1, y1 - y0 + 1))
        ImageDraw.Draw(layer).rounded_rectangle(
            (0, 0, x1 - x0, y1 - y0), self.s(radius), fill=fill, outline=outline,
            width=self.s(width) if outline else 0,
        )
        self.image.alpha_composite(layer, (x0, y0))

    def brand(self, box: Sequence[float], radius: int) -> None:
        """A block in the website's yellow to orange."""
        x0, y0, x1, y1 = (self.s(value) for value in box)
        size = (x1 - x0, y1 - y0)
        shade = Image.linear_gradient("L").resize(size)
        tile = Image.composite(Image.new("RGBA", size, _ORANGE), Image.new("RGBA", size, _YELLOW), shade)
        mask = Image.new("L", size)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), self.s(radius), fill=255)
        self.image.paste(tile, (x0, y0), mask)

    def line(self, points: Sequence[tuple[float, float]], colour: str, width: float) -> None:
        ImageDraw.Draw(self.image).line(
            [(self.s(x), self.s(y)) for x, y in points], fill=colour, width=self.s(width),
        )

    def pixels(self, box: Sequence[float], shade: Any, colour: str) -> None:
        """Square pixels in `colour` across `box`, lit by `shade(x, y)`, from
        0 for none to 1 for all, on one grid across the whole picture."""
        x0, y0, x1, y1 = box
        draw = ImageDraw.Draw(self.image)
        side = self.s(_PIXEL) - 2
        for row in range(math.ceil(y0 / _PIXEL), int(y1 / _PIXEL)):
            for column in range(math.ceil(x0 / _PIXEL), int(x1 / _PIXEL)):
                x, y = column * _PIXEL, row * _PIXEL
                if shade(x, y) * 64 > _PATTERN[row % 8][column % 8] + 0.5:
                    draw.rectangle((self.s(x), self.s(y), self.s(x) + side - 1, self.s(y) + side - 1),
                                   fill=colour)

    def shaded_text(self, x: float, y: float, text: str, size: int, width: float) -> float:
        """`text` filled top to bottom from the first fill colour to the
        second in square pixels; colour emoji keep their colours."""
        image, self.image = self.image, Image.new("RGBA", self.image.size)
        drawn = self.text(x, y, text, size, "#ffffff", "name", width=width)
        layer, self.image = self.image, image
        top, bottom = self.s(y - size * 0.95), self.s(y + size * 0.3)
        colours = self.colours["fill"]
        cell = self.s(2)
        rows = max(1, (bottom - top) // cell)
        tile = Image.new("RGBA", (8, rows))
        for row in range(rows):
            for column in range(8):
                lit = row / rows * 64 > _PATTERN[row % 8][column]
                tile.putpixel((column, row), Image.new("RGBA", (1, 1), colours[lit]).getpixel((0, 0)))
        tile = tile.resize((8 * cell, rows * cell), Image.Resampling.NEAREST)
        fill = Image.new("RGBA", layer.size, colours[1])
        for left in range(0, layer.width, tile.width):
            fill.paste(tile, (left, top))
        red, green, blue, alpha = layer.split()
        white = ImageChops.darker(ImageChops.darker(red, green), blue).point(lambda v: 255 if v > 235 else 0)
        text_mask = ImageChops.multiply(alpha, white)
        layer.putalpha(ImageChops.subtract(alpha, text_mask))
        self.image.alpha_composite(layer)
        self.image.paste(fill, (0, 0), text_mask)
        return drawn

    def star(self, x: float, y: float, on: bool) -> None:
        points = [
            (self.s(x + reach * math.cos(-math.pi / 2 + i * math.pi / 5)),
             self.s(y + reach * math.sin(-math.pi / 2 + i * math.pi / 5)))
            for i in range(10)
            for reach in [9 if i % 2 == 0 else 9 * 0.47]
        ]
        ImageDraw.Draw(self.image).polygon(points, fill=self.colours["star" if on else "off"])


def _number(value: int | None) -> str:
    return "–" if value is None else f"{value:,}"


def _signed(value: int) -> str:
    return f"+{value:,}" if value > 0 else f"−{-value:,}" if value < 0 else "0"


def _trophy_chart(canvas: _Canvas, box: tuple[int, int, int, int], day: Mapping[str, Any]) -> None:
    """The trophies from the Reset through each battle in time order, as a
    stepped line over a pixel-shaded area, a square on each battle."""
    colours = canvas.colours
    x0, y0, x1, y1 = box
    battles = sorted(
        [(battle, True) for battle in day["attacks"]] + [(battle, False) for battle in day["defenses"]],
        key=lambda pair: pair[0].get("time") or day["date"],
    )
    start = day["start_trophies"] or 0
    values = [start]
    for battle, _ in battles:
        values.append(values[-1] + battle["trophies"])
    times = [battle.get("time") for battle, _ in battles]
    if times and all(times):
        span = max((times[-1] - day["date"]).total_seconds() * 1.06, 3600)
        steps = [x0 + (x1 - x0) * (time - day["date"]).total_seconds() / span for time in times]
    else:
        steps = [x0 + (x1 - x0) * (index + 1) / (len(battles) + 1) for index in range(len(battles))]
    low, high = min(values), max(values)
    high = max(high, low + 40)
    def height(value: int) -> float:
        # The lowest point a third of the way up, the highest near the top.
        return y1 - (y1 - y0) * (0.3 + 0.62 * (value - low) / (high - low))
    def level(x: float) -> float:
        passed = sum(step <= x for step in steps)
        return height(values[passed])
    canvas.pixels(box, lambda x, y: 0 if y < level(x) else
                  0.1 + 0.6 * (1 - (y - level(x)) / max(1, y1 - level(x))) ** 2, colours["chart"])
    points = [(x0, height(values[0]))]
    for index, x in enumerate(steps):
        points += [(x, height(values[index])), (x, height(values[index + 1]))]
    points.append((x1, height(values[-1])))
    canvas.line(points, colours["chart"], 2.5)
    for index, (battle, attack) in enumerate(battles):
        x, y = steps[index], height(values[index + 1])
        if battle["new"]:
            canvas.block((x - 8, y - 8, x + 8, y + 8), None, colours["ink"], 2, 2)
        canvas.block((x - 4, y - 4, x + 4, y + 4),
                     _ORANGE if battle["new"] else colours["positive" if attack else "negative"], None, 0, 1)


def _dial(canvas: _Canvas, centre: tuple[float, float], day: Mapping[str, Any]) -> None:
    """The day's 8 attacks then 8 defenses around three quarters of a ring,
    each lit once played."""
    colours = canvas.colours
    x, y = centre
    shown = [(battle, True) for battle in day["attacks"][-_MOST:]], [(battle, False) for battle in day["defenses"][-_MOST:]]
    for index in range(2 * _MOST):
        attack = index < _MOST
        played = shown[0 if attack else 1]
        place = index % _MOST
        lit = place < len(played)
        colour = (_ORANGE if played[place][0]["new"] else colours["positive" if attack else "negative"]) if lit else colours["off"]
        for part in range(2):
            angle = math.radians(135 + 270 * (index + 0.25 + part * 0.5) / (2 * _MOST))
            for ring in range(3):
                reach = 78 - ring * 9
                px, py = x + reach * math.cos(angle), y + reach * math.sin(angle)
                canvas.block((px - 3, py - 3, px + 3, py + 3), colour, None, 0, 1)
    attacks, defenses = len(day["attacks"]), len(day["defenses"])
    canvas.text(x, y + 6, f"{attacks + defenses}", 40, colours["ink"], "name", "middle")
    canvas.text(x, y + 30, "PLAYED", 13, colours["muted"], "mono", "middle", spaced=True)
    canvas.text(x - 52, y + 82, f"ATT {attacks}", 13, colours["positive"], "mono", "middle", spaced=True)
    canvas.text(x + 52, y + 82, f"DEF {defenses}", 13, colours["negative"], "mono", "middle", spaced=True)


def _card(canvas: _Canvas, box: tuple[int, int, int, int], label: str, right: str = "") -> None:
    colours = canvas.colours
    x0, y0, x1, _ = box
    canvas.block(box, colours["card"], colours["line"], 1, 10)
    canvas.text(x0 + 20, y0 + 30, label, 14, colours["muted"], "mono", spaced=True)
    if right:
        canvas.text(x1 - 20, y0 + 30, right, 14, colours["faint"], "mono", "right", spaced=True)


def _change(canvas: _Canvas, x: float, y: float, change: int | None, good: bool, words: str) -> None:
    """The change since the Reset in mono, its arrow green when `good`."""
    colours = canvas.colours
    if change is None:
        canvas.text(x, y, "NOT KNOWN YET", 14, colours["faint"], "mono", spaced=True)
        return
    width = canvas.text(x, y, f"{'↑' if good else '↓'} {abs(change):,}", 16,
                        colours["positive" if good else "negative"], "mono")
    canvas.text(x + width + 12, y, words, 14, colours["muted"], "mono", spaced=True)


def _battle(canvas: _Canvas, box: tuple[int, int, int, int], battle: Mapping[str, Any],
            attack: bool) -> None:
    """A battle: its stars, destruction and opponent, then a pixel bar as
    long as its trophies of the most, 40, as dense as its destruction."""
    colours = canvas.colours
    x0, y0, x1, y1 = box
    new = battle["new"]
    middle = (y0 + y1) / 2
    if new:
        canvas.block((x0 + 6, y0 + 3, x1 - 6, y1 - 3), colours["new"], _ORANGE, 2, 6)
    for index in range(3):
        canvas.star(x0 + 26 + index * 20, middle - 1, index < battle["stars"])
    canvas.text(x0 + 132, middle + 6, f"{battle['destruction']}%", 15,
                colours["ink" if new else "muted"], "mono", "right")
    canvas.text(x0 + 146, middle + 7, battle["name"], 19, colours["ink"], "body", width=170)
    colour = colours["chart"] if new else colours["positive" if attack else "negative"]
    bar0, bar1 = x1 - 222, x1 - 74
    length = min(abs(battle["trophies"]), 40) / 40 * (bar1 - bar0)
    density = 0.3 + 0.7 * battle["destruction"] / 100
    canvas.pixels((bar0, middle - 8, bar0 + length, middle + 8), lambda _x, _y: density, colour)
    for dot in range(int(bar0 + length) + 8, int(bar1), 8):
        canvas.block((dot, middle - 1, dot + 2, middle + 1), colours["faint"], None, 0, 0)
    canvas.text(x1 - 18, middle + 8, _signed(battle["trophies"]), 22, colour, "name", "right")
    if new:
        canvas.brand((x0 + 70 - 52, y0 - 8, x0 + 70, y0 + 10), 4)
        canvas.text(x0 + 44, y0 + 6, "NEW", 12, _BRAND_NIGHT, "mono", "middle", spaced=True)


def legend_day(
    page: Mapping[str, Any], start: datetime, battle: str | None = None
) -> dict[str, Any] | None:
    """The player's Legend day from the Reset `start`, from the player page
    read, as `draw` takes it, with `battle` marked new; None when the page
    does not have that day. Its trophies go from the start to the start plus
    every battle listed, as the picture totals them, or are not known when
    the day may be missing battles, and its rank from the
    place on the board frozen at its Reset, the day before's closing place,
    to the live place now."""
    days = {
        datetime.fromisoformat(day["ranked_day_start"]): day
        for day in (page.get("screen_ready") or {}).get("days", ())
    }
    day = days.get(start)
    if day is None:
        return None
    before = days.get(start - timedelta(days=1)) or {}
    trophies = day["start_trophies"]

    def battles(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": event["opponent"]["name"] or event["opponent"]["tag"],
                "stars": event["stars"],
                "destruction": event["destruction_percentage"],
                "trophies": event["trophy_change"],
                "new": event["battle_id"] == battle,
                "time": event.get("battle_timestamp") and datetime.fromisoformat(
                    event["battle_timestamp"]),
            }
            for event in events
        ]

    attacks, defenses = battles(day["offense_events"]), battles(day["defense_events"])
    complete = day["battles_complete"]
    return {
        "name": page["name"] or page["tag"],
        "day_number": day["season_day_number"],
        "date": start,
        "complete": complete,
        "start_trophies": trophies,
        "trophies": None if trophies is None or not complete else trophies + sum(
            battle["trophies"] for battle in (*attacks, *defenses)
        ),
        "start_rank": before.get("reset_rank"),
        "rank": page["rank"],
        "attacks": attacks,
        "defenses": defenses,
    }


# Every picture is drawn on one thread kept for drawing, one at a time: each
# takes about 50 MB while it is drawn, and memory freed on a thread stays
# kept for that thread, so drawing on the bot's other threads in turn took
# the bot past the 384 MiB it may use. At most `_MOST_DRAWING` pictures are
# being drawn or waiting at once, and /stats takes at most `_MOST_FOR_STATS`
# of them, so attack alerts always have the rest; another is not drawn.
_DRAWER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="picture")
_MOST_DRAWING = 6
_MOST_FOR_STATS = 4
_DRAWING = threading.BoundedSemaphore(_MOST_DRAWING)
_FOR_STATS = threading.BoundedSemaphore(_MOST_FOR_STATS)


class Busy(Exception):
    """Too many pictures are being drawn or waiting already."""


def drawing(day: Mapping[str, Any], dark: bool, *, alert: bool = False) -> Future[bytes]:
    """`draw(day, dark)` on the drawing thread, for an attack alert or else
    for /stats, raising Busy when too many pictures wait for it. A picture
    cancelled before it starts is not drawn."""
    held = [_DRAWING] if alert else [_FOR_STATS, _DRAWING]
    taken: list[threading.BoundedSemaphore] = []
    try:
        for semaphore in held:
            if not semaphore.acquire(blocking=False):
                raise Busy
            taken.append(semaphore)
        future = _DRAWER.submit(lambda: draw(day, dark))
    except BaseException:
        for semaphore in taken:
            semaphore.release()
        raise

    def release(_done: Future[bytes]) -> None:
        for semaphore in taken:
            semaphore.release()

    future.add_done_callback(release)
    return future


def draw(day: Mapping[str, Any], dark: bool) -> bytes:
    """The PNG picture of `day` (`legend_day`): its Season day, the player,
    trophies and rank from the Reset to now, then each attack and defense
    with their totals, the new defense marked. The bot draws only through
    `drawing`."""
    canvas = _Canvas(_DARK if dark else _LIGHT)
    colours = canvas.colours

    canvas.shaded_text(30, 78, day["name"], 52, 760)
    canvas.text(32, 112, f"LEGEND LEAGUE · {day['date']:%-d %b %Y}".upper(), 15,
                colours["muted"], "mono", spaced=True)
    season = f"DAY {_number(day['day_number'])} / 28"
    width = _measure(season, canvas.s(16), "mono") / _SCALE + 24 + 16 * 0.14 * len(season)
    canvas.brand((WIDTH - 32 - width, 46, WIDTH - 32, 84), 6)
    canvas.text(WIDTH - 32 - width / 2, 71, season, 16, _BRAND_NIGHT, "mono", "middle", spaced=True)

    start, now = day["start_trophies"], day["trophies"]
    times = [battle["time"] for battle in (*day["attacks"], *day["defenses"]) if battle.get("time")]
    _card(canvas, (32, 140, 640, 360), "TROPHIES",
          f"{day['date']:%H:%M} - {max(times):%H:%M} UTC" if times else "")
    canvas.text(52, 216, _number(now), 54, colours["ink"], "name")
    _change(canvas, 54, 248, None if start is None or now is None else now - start,
            start is not None and now is not None and now >= start, f"FROM {_number(start)}")
    _trophy_chart(canvas, (52, 264, 620, 344), day)

    start, now = day["start_rank"], day["rank"]
    _card(canvas, (656, 140, 900, 360), "RANK")
    canvas.text(676, 216, f"#{_number(now)}", 54, colours["ink"], "name", width=210)
    _change(canvas, 678, 248, None if start is None or now is None else start - now,
            start is not None and now is not None and now <= start, "PLACES")
    canvas.text(678, 330, "AT RESET", 13, colours["faint"], "mono", spaced=True)
    canvas.text(678, 344 - 40, f"#{_number(start)}", 26, colours["muted"], "name")

    _card(canvas, (916, 140, 1168, 360), "BATTLES")
    _dial(canvas, (1042, 258), day)

    for attack, battles, x0, title in (
        (True, day["attacks"], 32, "ATTACKS"),
        (False, day["defenses"], 608, "DEFENSES"),
    ):
        x1 = x0 + 560
        shown = battles[-_MOST:]
        _card(canvas, (x0, 376, x1, 812), title, f"{len(battles)} / {_MOST}")
        total = sum(battle["trophies"] for battle in battles)
        canvas.text(x0 + 20 + _measure(title, canvas.s(14), "mono") / _SCALE + 30, 408,
                    _signed(total), 22, colours["positive" if attack else "negative"], "name")
        canvas.line([(x0 + 1, 424), (x1 - 1, 424)], colours["line"], 1)
        # A day with more than 8 battles shows its last 8, the new defense among them.
        for index in range(_MOST):
            box = (x0, 430 + index * 47, x1, 472 + index * 47)
            if index < len(shown):
                _battle(canvas, box, shown[index], attack)
            else:
                middle = (box[1] + box[3]) / 2
                for dot in range(x0 + 24, x1 - 24, 8):
                    canvas.block((dot, middle - 1, dot + 2, middle + 1), colours["off"], None, 0, 0)
                canvas.block(((x0 + x1) / 2 - 64, middle - 10, (x0 + x1) / 2 + 64, middle + 10),
                             colours["card"], None, 0, 0)
                canvas.text((x0 + x1) / 2, middle + 5, "NOT PLAYED" if day["complete"] else "–", 13,
                            colours["faint"], "mono", "middle", spaced=True)

    canvas.text(32, 842, "CLASHLENS.NET", 14, colours["faint"], "mono", spaced=True)
    word = _measure("Clash Lens", canvas.s(20), "name") / _SCALE
    with Image.open(_FONTS / "mark.png") as mark:
        canvas.image.alpha_composite(
            mark.convert("RGBA").resize((canvas.s(28), canvas.s(28))),
            (canvas.s(WIDTH - 32 - word - 36), canvas.s(822)),
        )
    canvas.text(WIDTH - 32, 844, "Clash Lens", 20, colours["ink"], "name", "right")
    out = io.BytesIO()
    canvas.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).convert("RGB").save(
        out, "PNG", optimize=True
    )
    return out.getvalue()
