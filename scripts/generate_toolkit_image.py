#!/usr/bin/env python3
"""Compose the README toolkit badges into a print-sized SVG strip."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPOSITORY_ROOT / "README.md"
ALLOWED_REMOTE_HOSTS = {"img.shields.io"}
IMAGE_TAG_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ATTRIBUTE_PATTERN = re.compile(
    r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL
)
SVG_TAG_PATTERN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)(?:px)?\s*$")


@dataclass(frozen=True)
class Badge:
    label: str
    source: str
    svg: str
    width: float
    height: float


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "dist" / "toolkit-letter.svg",
        help="Destination SVG path.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Target print resolution used to calculate physical dimensions.",
    )
    parser.add_argument(
        "--content-width-inches",
        type=float,
        default=7.5,
        help="Usable page width. The default fits US Letter with 0.5-inch margins.",
    )
    parser.add_argument(
        "--badge-height-pixels",
        type=int,
        default=66,
        help="Badge height in the final raster image.",
    )
    parser.add_argument("--horizontal-gap-pixels", type=int, default=18)
    parser.add_argument("--vertical-gap-pixels", type=int, default=18)
    parser.add_argument("--vertical-padding-pixels", type=int, default=24)
    return parser.parse_args()


def toolkit_image_entries(readme: str) -> list[tuple[str, str]]:
    heading_match = re.search(r"^##\s+\d+\s+/\s+Toolkit\s*$", readme, re.MULTILINE)
    if heading_match is None:
        raise ValueError(f"Could not find the numbered Toolkit heading in {README_PATH}")

    start = heading_match.start()
    next_heading = readme.find("\n## ", heading_match.end())
    section = readme[start : next_heading if next_heading != -1 else len(readme)]

    entries: list[tuple[str, str]] = []
    for tag in IMAGE_TAG_PATTERN.findall(section):
        attributes = {
            name.lower(): html.unescape(value)
            for name, _, value in ATTRIBUTE_PATTERN.findall(tag)
        }
        source = attributes.get("src")
        label = attributes.get("alt")
        if source and label:
            entries.append((label, source))

    if not entries:
        raise ValueError("The Toolkit section does not contain any labeled images")
    return entries


def fetch_remote_svg(source: str) -> str:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_REMOTE_HOSTS:
        raise ValueError(
            f"Remote badge host is not allowed: {source}. "
            f"Allowed hosts: {', '.join(sorted(ALLOWED_REMOTE_HOSTS))}"
        )

    request = urllib.request.Request(
        source,
        headers={"User-Agent": "bingogome-toolkit-image-generator/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {source}: {last_error}")


def local_svg(source: str) -> str:
    path = (REPOSITORY_ROOT / source).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise ValueError(f"Local badge escapes the repository: {source}") from error
    if not path.is_file():
        raise FileNotFoundError(f"Local badge does not exist: {source}")
    return path.read_text(encoding="utf-8")


def numeric_dimension(value: str | None) -> float | None:
    if value is None:
        return None
    match = NUMBER_PATTERN.match(value)
    return float(match.group(1)) if match else None


def svg_dimensions(svg: str, source: str) -> tuple[float, float]:
    tag_match = SVG_TAG_PATTERN.search(svg)
    if not tag_match:
        raise ValueError(f"Badge is not an SVG: {source}")
    attributes = {
        name.lower(): value
        for name, _, value in ATTRIBUTE_PATTERN.findall(tag_match.group(0))
    }
    width = numeric_dimension(attributes.get("width"))
    height = numeric_dimension(attributes.get("height"))
    if width and height:
        return width, height

    view_box = attributes.get("viewbox", "").replace(",", " ").split()
    if len(view_box) == 4:
        return float(view_box[2]), float(view_box[3])
    raise ValueError(f"Could not determine SVG dimensions for {source}")


def load_badge(entry: tuple[str, str]) -> Badge:
    label, source = entry
    svg = fetch_remote_svg(source) if source.startswith("https://") else local_svg(source)
    if "<svg" not in svg.lower():
        raise ValueError(f"Badge source did not return SVG markup: {source}")
    width, height = svg_dimensions(svg, source)
    return Badge(label=label, source=source, svg=svg, width=width, height=height)


def compose_svg(
    badges: list[Badge],
    *,
    dpi: int,
    content_width_inches: float,
    badge_height: int,
    horizontal_gap: int,
    vertical_gap: int,
    vertical_padding: int,
) -> str:
    canvas_width = round(content_width_inches * dpi)
    x = 0
    y = vertical_padding
    placements: list[tuple[Badge, int, int, int]] = []

    for badge in badges:
        rendered_width = round(badge.width * badge_height / badge.height)
        if rendered_width > canvas_width:
            raise ValueError(
                f"Badge {badge.label!r} is wider than the {canvas_width}px canvas"
            )
        if x and x + rendered_width > canvas_width:
            x = 0
            y += badge_height + vertical_gap
        placements.append((badge, x, y, rendered_width))
        x += rendered_width + horizontal_gap

    canvas_height = y + badge_height + vertical_padding
    physical_height = canvas_height / dpi
    images: list[str] = []
    for badge, image_x, image_y, image_width in placements:
        encoded_svg = base64.b64encode(badge.svg.encode("utf-8")).decode("ascii")
        label = html.escape(badge.label, quote=True)
        images.append(
            f'  <g role="img" aria-label="{label}">\n'
            f"    <title>{html.escape(badge.label)}</title>\n"
            f'    <image x="{image_x}" y="{image_y}" width="{image_width}" '
            f'height="{badge_height}" href="data:image/svg+xml;base64,{encoded_svg}"/>\n'
            "  </g>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{content_width_inches:g}in" '
        f'height="{physical_height:.4f}in" viewBox="0 0 {canvas_width} {canvas_height}" '
        'role="img" aria-labelledby="toolkit-title toolkit-description">\n'
        '  <title id="toolkit-title">Toolkit</title>\n'
        '  <desc id="toolkit-description">Technology badges composed for a US Letter CV.</desc>\n'
        + "\n".join(images)
        + "\n</svg>\n"
    )


def main() -> int:
    arguments = parse_arguments()
    if arguments.dpi <= 0 or arguments.content_width_inches <= 0:
        raise ValueError("DPI and content width must be positive")
    if arguments.badge_height_pixels <= 0:
        raise ValueError("Badge height must be positive")

    entries = toolkit_image_entries(README_PATH.read_text(encoding="utf-8"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        badges = list(executor.map(load_badge, entries))

    svg = compose_svg(
        badges,
        dpi=arguments.dpi,
        content_width_inches=arguments.content_width_inches,
        badge_height=arguments.badge_height_pixels,
        horizontal_gap=arguments.horizontal_gap_pixels,
        vertical_gap=arguments.vertical_gap_pixels,
        vertical_padding=arguments.vertical_padding_pixels,
    )
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    print(
        f"Generated {output} with {len(badges)} badges at "
        f"{arguments.content_width_inches:g} inches wide ({arguments.dpi} DPI)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
