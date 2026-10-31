"""The picture of a player's Legend day so far, at the bottom of an attack
alert and in /stats, in the website's colours and fonts, light unless the
Clasher chose dark on the website's Discord alerts page.

Pillow draws with one font at a time, so text is split into the characters a
reader sees, each drawn with the first font that has it: the website's Inter
and Barlow Condensed (copied into `fonts`), then Noto for other scripts and
symbols, then Noto's colour emoji. The container image carries Noto (see
python/Containerfile); a Noto file that is missing is skipped.
"""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import regex
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

_FONTS = Path(__file__).parent / "fonts"
_NOTO = Path("/usr/share/fonts/truetype/noto")
_CJK = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
# Each font: its file, its place in a font collection, and the weight to use
# from a file that holds every weight.
_FACES: dict[str, tuple[Path, int, int | None]] = {
    "name": (_FONTS / "inter-latin.woff2", 0, 600),
    "label": (_FONTS / "inter-latin.woff2", 0, 700),
    "score": (_FONTS / "barlow-condensed-700-latin.woff2", 0, None),
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
_WEBSITE = ("name", "label", "score")
_FALLBACK = tuple(name for name in _FACES if name not in _WEBSITE)
_EMOJI = _NOTO / "NotoColorEmoji.ttf"
_EMOJI_SIZE = 109  # the only size the colour emoji font draws at
# A character no font has, so a font draws its box for a missing one.
_MISSING = "\U0010fffd"
_INVISIBLE = frozenset("\u200d\ufe0f\ufe0e\u200b\u200c\u2060")
# The website's Inter features (--cl-font in website/app/theme.css), which
# need Pillow's text layout library.
_INTER_FEATURES = ["cv11", "ss03"] if features.check_feature("raqm") else None

# The website's colours (website/app/theme.css and appearance.css).
_YELLOW, _ORANGE, _BRAND_NIGHT = "#ffd447", "#ff8a1f", "#1a1206"
_LIGHT = {
    "paper": "#f4f4f5", "row": "#ffffff", "outline": "#e6e6e9", "ink": "#0b0b0d",
    "muted": "#5b5b65", "faint": "#66666f", "star": "#e8a200", "no_star": "#d4d4d8",
    "positive": "#13773a", "negative": "#c81e1e", "positive_soft": "#e7f6ec",
    "negative_soft": "#fdebeb", "new": "#fff4e8", "empty": "#ececef",
    "empty_outline": "#e6e6e9", "empty_text": "#a1a1aa", "glow": 34,
}
_DARK = {
    "paper": "#050506", "row": "#141417", "outline": "#2e2e33", "ink": "#f4f4f5",
    "muted": "#a1a1aa", "faint": "#8d8d96", "star": "#ffc53d", "no_star": "#3a3a40",
    "positive": "#4ade80", "negative": "#ff6b5b", "positive_soft": "#4ade8028",
    "negative_soft": "#ff6b5b28", "new": "#ff8a1f1e", "empty": "#0e0e10",
    "empty_outline": "#1d1d21", "empty_text": "#46464d", "glow": 26,
}
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


@cache
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


def _features(name: str) -> list[str] | None:
    return _INTER_FEATURES if name in ("name", "label") else None


def _run_width(name: str, text: str, size: int) -> float:
    if name == "emoji":
        return _emoji().getlength(text) * size * 1.05 / _EMOJI_SIZE  # type: ignore[union-attr]
    return _face(name, size).getlength(text, features=_features(name))  # type: ignore[union-attr]


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
        anchor: str = "left", width: float | None = None,
    ) -> float:
        """Draw `text` with its baseline at `y`, starting, centred or ending
        at `x` by `anchor`, cut short to `width`; returns its width."""
        size = self.s(size)
        if width is not None:
            text = _fit(text, size, self.s(width), style)
        runs = _runs(text, style)
        total = sum(_run_width(name, part, size) for name, part in runs)
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
                          anchor="ls", features=_features(name))
            left += _run_width(name, part, size)
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

    def star(self, x: float, y: float, on: bool) -> None:
        points = [
            (self.s(x + reach * math.cos(-math.pi / 2 + i * math.pi / 5)),
             self.s(y + reach * math.sin(-math.pi / 2 + i * math.pi / 5)))
            for i in range(10)
            for reach in [11 if i % 2 == 0 else 11 * 0.47]
        ]
        ImageDraw.Draw(self.image).polygon(
            points, fill=self.colours["star" if on else "no_star"]
        )


def _number(value: int | None) -> str:
    return "–" if value is None else f"{value:,}"


def _signed(value: int) -> str:
    return f"+{value:,}" if value > 0 else f"−{-value:,}" if value < 0 else "0"


def _battle(canvas: _Canvas, box: tuple[int, int, int, int], battle: Mapping[str, Any],
            attack: bool) -> None:
    colours = canvas.colours
    x0, y0, x1, y1 = box
    new = battle["new"]
    canvas.block(box, colours["new"] if new else colours["row"], None if new else colours["outline"])
    if new:
        canvas.block((x0 - 3, y0 - 3, x1 + 3, y1 + 3), None, _ORANGE, 3, 14)
    middle = (y0 + y1) / 2
    for index in range(3):
        canvas.star(x0 + 24 + index * 25, middle - 1, index < battle["stars"])
    canvas.text(x0 + 150, middle + 8, f"{battle['destruction']}%", 20,
                colours["ink" if new else "muted"], "label", "right")
    canvas.text(x0 + 166, middle + 9, battle["name"], 23, colours["ink"],
                width=(x1 - x0) - 166 - 100)
    canvas.text(x1 - 18, middle + 12, _signed(battle["trophies"]), 36,
                colours["positive" if attack else "negative"], "score", "right")
    if new:
        canvas.brand((x1 - 64, y0 - 13, x1 - 6, y0 + 9), 8)
        canvas.text(x1 - 35, y0 + 4, "NEW", 15, _BRAND_NIGHT, "label", "middle")


def _change(canvas: _Canvas, x0: int, label: str, start: str, now: str,
            change: str | None, good: bool) -> None:
    """A block from the start of the day to now, with the change."""
    colours = canvas.colours
    x1 = x0 + 560
    canvas.block((x0, 104, x1, 214), colours["row"], colours["outline"], 2, 16)
    canvas.text(x0 + 24, 138, label, 17, colours["faint"], "label")
    canvas.text(x0 + 24, 196, "START", 15, colours["faint"], "label")
    start_width = canvas.text(x0 + 92, 198, start, 46, colours["muted"], "score")
    canvas.text(x0 + 92 + start_width + 14, 194, "→", 30, colours["faint"], "label")
    canvas.text(x0 + 92 + start_width + 56, 198, now, 58, colours["ink"], "score")
    if change is not None:
        chip = _measure(change, canvas.s(30), "score") / _SCALE + 28
        canvas.block((x1 - 24 - chip, 152, x1 - 24, 194),
                     colours["positive_soft" if good else "negative_soft"], None, 0, 10)
        canvas.text(x1 - 24 - chip / 2, 184, change, 30,
                    colours["positive" if good else "negative"], "score", "middle")


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


# One picture at a time: each takes about 50 MB while it is drawn (5 at once
# peaked at 356 MB), and the bot may use 384 MiB.
_ONE_AT_A_TIME = threading.Lock()


def draw(day: Mapping[str, Any], dark: bool) -> bytes:
    """The PNG picture of `day` (`legend_day`): its Season day, the player,
    trophies and rank from the Reset to now, then each attack and defense
    with their totals, the new defense marked."""
    with _ONE_AT_A_TIME:
        return _draw(day, dark)


def _draw(day: Mapping[str, Any], dark: bool) -> bytes:
    canvas = _Canvas(_DARK if dark else _LIGHT)
    colours = canvas.colours
    glow = Image.new("RGBA", canvas.image.size)
    ImageDraw.Draw(glow).ellipse(
        tuple(canvas.s(value) for value in (-300, -420, 700, 260)),
        fill=_ORANGE + f"{colours['glow']:02x}",
    )
    canvas.image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(canvas.s(120))))

    canvas.brand((32, 30, 182, 82), 14)
    canvas.text(107, 66, f"DAY {_number(day['day_number'])}", 30, _BRAND_NIGHT, "score", "middle")
    canvas.text(202, 68, day["name"], 38, colours["ink"], "label", width=600)
    canvas.text(WIDTH - 32, 66, f"{day['date']:%-d %b}", 22, colours["muted"], "label", "right")

    start, now = day["start_trophies"], day["trophies"]
    known = start is not None and now is not None
    _change(canvas, 32, "TROPHIES", _number(start), _number(now),
            _signed(now - start) if known else None, known and now >= start)
    start, now = day["start_rank"], day["rank"]
    known = start is not None and now is not None
    _change(canvas, 608, "RANK", f"#{_number(start)}", f"#{_number(now)}",
            f"{'▲' if now <= start else '▼'}{abs(now - start):,}" if known else None,
            known and now <= start)

    for attack, battles, x0, title in (
        (True, day["attacks"], 32, "ATTACKS"),
        (False, day["defenses"], 608, "DEFENSES"),
    ):
        x1 = x0 + 560
        canvas.text(x0 + 4, 268, title, 22, colours["muted"], "label")
        canvas.text(x0 + 16 + _measure(title, canvas.s(22), "label") / _SCALE, 268,
                    f"{len(battles)} of {_MOST}", 18, colours["faint"], "label")
        canvas.text(x1 - 4, 272, _signed(sum(battle["trophies"] for battle in battles)), 46,
                    colours["positive" if attack else "negative"], "score", "right")
        # A day with more than 8 battles shows its last 8, the new defense among them.
        shown = battles[-_MOST:]
        for index in range(_MOST):
            box = (x0, 290 + index * 62, x1, 342 + index * 62)
            if index < len(shown):
                _battle(canvas, box, shown[index], attack)
            else:
                canvas.block(box, colours["empty"], colours["empty_outline"])
                canvas.text((x0 + x1) / 2, box[1] + 34,
                            "not played" if day["complete"] else "–", 20,
                            colours["empty_text"], "label", "middle")

    canvas.text(32, 832, "clashlens.net", 18, colours["faint"], "label")
    word = _measure("Clash Lens", canvas.s(22), "label") / _SCALE
    with Image.open(_FONTS / "mark.png") as mark:
        canvas.image.alpha_composite(
            mark.convert("RGBA").resize((canvas.s(40), canvas.s(40))),
            (canvas.s(WIDTH - 32 - word - 50), canvas.s(806)),
        )
    canvas.text(WIDTH - 32, 834, "Clash Lens", 22, colours["ink"], "label", "right")
    out = io.BytesIO()
    canvas.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).convert("RGB").save(
        out, "PNG", optimize=True
    )
    return out.getvalue()
