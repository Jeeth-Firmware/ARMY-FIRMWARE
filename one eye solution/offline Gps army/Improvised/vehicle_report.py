#!/usr/bin/env python3
"""
Vehicle_Report.pdf for USB exports — pure Python, no extra packages.

Event JPEGs are embedded unchanged (PDF DCTDecode), so building the report
costs almost no CPU or RAM on the Pi Zero.
"""
from __future__ import annotations

import os
import struct
import zlib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PAGE_W, PAGE_H = 595.0, 842.0  # A4, points
MARGIN = 40.0
CONTENT_W = PAGE_W - 2 * MARGIN
FOOTER_Y = PAGE_H - 26

IMAGES_PER_TYPE = int(os.getenv("USB_REPORT_IMAGES_PER_TYPE", "4"))

EVENT_LABELS = {
    "Sleeping": "Sleeping",
    "Yawning": "Yawning",
    "PhoneCall": "Phone Call",
    "LookingDown": "Looking Down",
    "LookingAway": "Looking Away",
    "NoFace": "No Face",
    "Overspeeding": "Overspeeding",
    "Incomplete": "Incomplete",
}
EVENT_ORDER = list(EVENT_LABELS)
EVENT_COLORS = {
    "Sleeping": (0.84, 0.19, 0.19),
    "Yawning": (0.96, 0.60, 0.04),
    "PhoneCall": (0.55, 0.27, 0.85),
    "LookingDown": (0.13, 0.52, 0.85),
    "LookingAway": (0.08, 0.64, 0.60),
    "NoFace": (0.45, 0.47, 0.52),
    "Overspeeding": (0.90, 0.35, 0.13),
}
DEFAULT_COLOR = (0.60, 0.62, 0.66)
NAVY = (0.10, 0.14, 0.26)
ACCENT = (0.30, 0.43, 0.96)
GRAY = (0.42, 0.45, 0.50)
LIGHT = (0.95, 0.96, 0.98)
BORDER = (0.85, 0.87, 0.90)
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.12, 0.12, 0.14)
RED = (0.80, 0.20, 0.20)

# Helvetica advance widths (1/1000 em) for ASCII 32..126
_HELV_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015,
    667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778,
    722, 667, 611, 722, 667, 944, 667, 667, 611,
    278, 278, 278, 469, 556, 333,
    556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556,
    333, 500, 278, 556, 500, 722, 500, 500, 500,
    334, 260, 334, 584,
)
assert len(_HELV_WIDTHS) == 95


def text_width(text: str, size: float, bold: bool = False) -> float:
    total = 0
    for ch in text:
        code = ord(ch)
        total += _HELV_WIDTHS[code - 32] if 32 <= code < 127 else 556
    # Helvetica-Bold is slightly wider; close enough for layout
    return total * size / 1000.0 * (1.06 if bold else 1.0)


def fit_text(text: str, size: float, max_w: float, bold: bool = False) -> str:
    if text_width(text, size, bold) <= max_w:
        return text
    while text and text_width(text + "...", size, bold) > max_w:
        text = text[:-1]
    return text + "..."


def _pdf_str(text) -> bytes:
    raw = " ".join(str(text).split()).encode("cp1252", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(data: bytes) -> tuple[int, int, int]:
    """(width, height, components) from a JPEG's SOF segment."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if marker in _SOF_MARKERS and i + 10 <= len(data):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            if width and height:
                return width, height, data[i + 9]
            break
        i += 2 + length
    raise ValueError("JPEG size not found")


class _Page:
    def __init__(self):
        self.ops: list[bytes] = []
        self.images: set[int] = set()

    @staticmethod
    def _color(rgb, stroke=False) -> bytes:
        return b"%.3f %.3f %.3f %s" % (rgb[0], rgb[1], rgb[2], b"RG" if stroke else b"rg")

    def rect(self, x, y, w, h, fill=None, stroke=None, width=0.6):
        """Rectangle with (x, y) = top-left, y measured down from the page top."""
        parts = [b"q"]
        if fill:
            parts.append(self._color(fill))
        if stroke:
            parts += [self._color(stroke, True), b"%.2f w" % width]
        parts.append(b"%.2f %.2f %.2f %.2f re" % (x, PAGE_H - y - h, w, h))
        parts.append(b"B" if fill and stroke else b"f" if fill else b"S")
        parts.append(b"Q")
        self.ops.append(b" ".join(parts))

    def line(self, x1, y1, x2, y2, color=BORDER, width=0.6):
        self.ops.append(
            b"q " + self._color(color, True)
            + b" %.2f w %.2f %.2f m %.2f %.2f l S Q" % (width, x1, PAGE_H - y1, x2, PAGE_H - y2)
        )

    def text(self, x, y, text, size=10.0, bold=False, color=BLACK, align="left"):
        """Text with its baseline at y (measured down from the page top)."""
        text = " ".join(str(text).split())
        if align != "left":
            w = text_width(text, size, bold)
            x = x - w if align == "right" else x - w / 2
        self.ops.append(
            b"BT /F%d %.1f Tf " % (2 if bold else 1, size) + self._color(color)
            + b" %.2f %.2f Td " % (x, PAGE_H - y) + _pdf_str(text) + b" Tj ET"
        )

    def image(self, index, x, y, w, h):
        self.images.add(index)
        self.ops.append(b"q %.2f 0 0 %.2f %.2f %.2f cm /Im%d Do Q" % (w, h, x, PAGE_H - y - h, index))


class PdfDocument:
    def __init__(self, title: str = ""):
        self.title = title
        self.pages: list[_Page] = []
        self._images: list[tuple[bytes, int, int, int]] = []

    def new_page(self) -> _Page:
        page = _Page()
        self.pages.append(page)
        return page

    def add_jpeg(self, data: bytes) -> tuple[int, int, int]:
        """Register a JPEG; returns (image index, width, height)."""
        width, height, comps = jpeg_info(data)
        if comps not in (1, 3):
            raise ValueError(f"unsupported JPEG with {comps} components")
        self._images.append((data, width, height, comps))
        return len(self._images) - 1, width, height

    def to_bytes(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")
        pages_obj = add(b"")
        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

        image_objs = []
        for data, width, height, comps in self._images:
            colorspace = b"/DeviceRGB" if comps == 3 else b"/DeviceGray"
            image_objs.append(add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
                b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                % (width, height, colorspace, len(data)) + data + b"\nendstream"
            ))

        page_objs = []
        for page in self.pages:
            content = zlib.compress(b"\n".join(page.ops))
            content_obj = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content)
                              + content + b"\nendstream")
            xobjects = b" ".join(b"/Im%d %d 0 R" % (i, image_objs[i]) for i in sorted(page.images))
            resources = b"<< /Font << /F1 %d 0 R /F2 %d 0 R >>" % (font_regular, font_bold)
            if xobjects:
                resources += b" /XObject << " + xobjects + b" >>"
            resources += b" >>"
            page_objs.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R >>"
                % (pages_obj, int(PAGE_W), int(PAGE_H), resources, content_obj)
            ))

        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
        objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % n for n in page_objs), len(page_objs))
        info = add(b"<< /Title " + _pdf_str(self.title) + b" /Producer (BlinkSmart offline export)"
                   b" /CreationDate (D:" + datetime.now().strftime("%Y%m%d%H%M%S").encode() + b") >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref_at = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, catalog, info, xref_at))
        return bytes(out)


# ---------------------------------------------------------------- report ---

def _label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type)


def _type_order(event_type: str):
    return (EVENT_ORDER.index(event_type) if event_type in EVENT_ORDER else len(EVENT_ORDER), event_type)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_time(ts, fmt="%d %b %Y %H:%M") -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime(fmt)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def pick_evenly(items: list, k: int) -> list:
    """k items spread across the list (first, ..., last)."""
    n = len(items)
    if k <= 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[n // 2]]
    return [items[round(i * (n - 1) / (k - 1))] for i in range(k)]


def _snapshot_candidates(folder: Path) -> list[Path]:
    """Middle frame first (the event itself, not the pre/post context), then neighbours."""
    frames = sorted(folder.glob("frame_*.jpg"))
    mid = len(frames) // 2
    return [frames[mid]] + frames[mid + 1:mid + 2] + frames[max(0, mid - 1):mid] if frames else []


class _Layout:
    def __init__(self, doc: PdfDocument):
        self.doc = doc
        self.page: _Page | None = None
        self.y = MARGIN

    def new_page(self, title: str | None = None) -> _Page:
        self.page = self.doc.new_page()
        self.y = MARGIN
        if title:
            self.page.text(MARGIN, self.y + 14, title, 15, True, NAVY)
            self.page.line(MARGIN, self.y + 22, PAGE_W - MARGIN, self.y + 22, NAVY, 1.2)
            self.y += 38
        return self.page

    def ensure(self, height: float, title: str | None = None) -> bool:
        """Start a new page if `height` doesn't fit. Returns True if a page was added."""
        if self.y + height > FOOTER_Y - 16:
            self.new_page(title)
            return True
        return False

    def heading(self, text: str):
        self.ensure(60)
        self.page.text(MARGIN, self.y + 12, text, 13, True, NAVY)
        self.y += 22


def _draw_cover(layout: _Layout, vehicle: dict, events: list[dict], trips: dict | None, generated: float):
    page = layout.new_page()
    page.rect(0, 0, PAGE_W, 96, fill=NAVY)
    page.text(MARGIN, 46, "Vehicle Safety Report", 24, True, WHITE)
    page.text(MARGIN, 68, "Driver monitoring summary - offline USB export", 10, color=(0.78, 0.82, 0.92))
    page.text(PAGE_W - MARGIN, 46, "Generated", 8, color=(0.78, 0.82, 0.92), align="right")
    page.text(PAGE_W - MARGIN, 61, _fmt_time(generated), 10, True, WHITE, align="right")
    layout.y = 116

    starts = sorted(s for s in (_to_float(e.get("start")) for e in events) if s)
    period = (f"{_fmt_time(starts[0], '%d %b %Y')} - {_fmt_time(starts[-1], '%d %b %Y')}"
              if starts else "No events")
    number = vehicle.get("vehicle_number") or ""

    y = layout.y
    page.rect(MARGIN, y, CONTENT_W, 80, fill=LIGHT, stroke=BORDER)
    page.text(MARGIN + 16, y + 22, "VEHICLE NUMBER", 8, True, GRAY)
    page.text(MARGIN + 16, y + 54, fit_text(number or "Not set", 22, 220, True), 22, True,
              BLACK if number else RED)
    col = MARGIN + 255
    rows = [("Driver", vehicle.get("driver_name") or "-"),
            ("Device serial", vehicle.get("device_serial") or "-"),
            ("Events period", period)]
    for i, (key, value) in enumerate(rows):
        row_y = y + 24 + i * 20
        page.text(col, row_y, key, 8, True, GRAY)
        page.text(col + 78, row_y, fit_text(value, 10, PAGE_W - MARGIN - col - 90), 10)
    layout.y += 98

    counts = Counter(e["type"] for e in events)
    top_type, top_count = counts.most_common(1)[0] if counts else ("", 0)
    days = len({_fmt_time(s, "%Y-%m-%d") for s in starts})
    trip_list = (trips or {}).get("trips") or []
    distance = sum(_to_float(t.get("total_distance_km")) or 0.0 for t in trip_list)
    tiles = [
        (str(len(events)), "Total events", ACCENT),
        (f"{_label(top_type)} ({top_count})" if top_type else "-", "Most frequent event",
         EVENT_COLORS.get(top_type, DEFAULT_COLOR)),
        (str(days), "Days with events", (0.18, 0.62, 0.27)),
        (f"{distance:,.0f} km" if trip_list else "-",
         f"{len(trip_list)} GPS trips recorded" if trip_list else "No GPS trips", (0.96, 0.60, 0.04)),
    ]
    tile_w = (CONTENT_W - 3 * 10) / 4
    for i, (value, label, color) in enumerate(tiles):
        x = MARGIN + i * (tile_w + 10)
        page.rect(x, layout.y, tile_w, 62, fill=WHITE, stroke=BORDER)
        page.rect(x, layout.y, 4, 62, fill=color)
        size = 18 if value.replace(",", "").replace(" km", "").isdigit() else 12
        page.text(x + 14, layout.y + 32, fit_text(value, size, tile_w - 20, True), size, True)
        page.text(x + 14, layout.y + 50, fit_text(label, 8, tile_w - 20), 8, color=GRAY)
    layout.y += 84


def _draw_type_table(layout: _Layout, events: list[dict]):
    layout.heading("Events by type")
    counts = Counter(e["type"] for e in events)
    page = layout.page
    if not counts:
        page.text(MARGIN, layout.y + 12, "No events were recorded in this export.", 10, color=GRAY)
        layout.y += 28
        return
    total = sum(counts.values())
    top = max(counts.values())
    count_right, share_right, bar_x = MARGIN + 250, MARGIN + 312, MARGIN + 330
    bar_max = PAGE_W - MARGIN - bar_x - 8

    page.rect(MARGIN, layout.y, CONTENT_W, 20, fill=LIGHT)
    page.text(MARGIN + 26, layout.y + 14, "Event type", 8, True, GRAY)
    page.text(count_right, layout.y + 14, "Count", 8, True, GRAY, align="right")
    page.text(share_right, layout.y + 14, "Share", 8, True, GRAY, align="right")
    layout.y += 20
    for event_type, count in sorted(counts.items(), key=lambda kv: (-kv[1], _type_order(kv[0]))):
        color = EVENT_COLORS.get(event_type, DEFAULT_COLOR)
        y = layout.y
        page.rect(MARGIN + 8, y + 6, 10, 10, fill=color)
        page.text(MARGIN + 26, y + 15, _label(event_type), 10)
        page.text(count_right, y + 15, str(count), 10, True, align="right")
        page.text(share_right, y + 15, f"{count * 100.0 / total:.0f}%", 10, color=GRAY, align="right")
        page.rect(bar_x, y + 6, max(2.0, bar_max * count / top), 10, fill=color)
        page.line(MARGIN, y + 22, PAGE_W - MARGIN, y + 22)
        layout.y += 22
    layout.y += 14


def _draw_hour_chart(layout: _Layout, events: list[dict]):
    starts = [s for s in (_to_float(e.get("start")) for e in events) if s]
    if not starts:
        return
    hours = Counter(datetime.fromtimestamp(s).hour for s in starts)
    layout.ensure(130)
    layout.heading("Events by time of day")
    page = layout.page
    chart_h, base = 70.0, layout.y + 82
    slot = CONTENT_W / 24
    top = max(hours.values())
    page.line(MARGIN, base, PAGE_W - MARGIN, base, GRAY, 0.8)
    for hour in range(24):
        x = MARGIN + hour * slot
        count = hours.get(hour, 0)
        if count:
            height = max(2.0, chart_h * count / top)
            night = hour >= 22 or hour < 6
            page.rect(x + 3, base - height, slot - 6, height, fill=(0.36, 0.28, 0.75) if night else ACCENT)
            page.text(x + slot / 2, base - height - 3, str(count), 7, color=GRAY, align="center")
        if hour % 3 == 0:
            page.text(x + slot / 2, base + 11, f"{hour:02d}:00", 7, color=GRAY, align="center")
    layout.y = base + 18
    page.text(MARGIN, layout.y + 6, "Purple bars: night hours (22:00-06:00), when drowsiness risk is highest.",
              8, color=GRAY)
    layout.y += 22


def _draw_daily_table(layout: _Layout, events: list[dict]):
    by_day: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        day = _fmt_time(e.get("start"), "%Y-%m-%d") or "Unknown date"
        by_day[day][e["type"]] += 1
    if not by_day:
        return

    def header():
        page = layout.page
        page.rect(MARGIN, layout.y, CONTENT_W, 20, fill=LIGHT)
        page.text(MARGIN + 10, layout.y + 14, "Date", 8, True, GRAY)
        page.text(MARGIN + 130, layout.y + 14, "Total", 8, True, GRAY, align="right")
        page.text(MARGIN + 150, layout.y + 14, "Breakdown", 8, True, GRAY)
        layout.y += 20

    layout.heading("Events per day")
    header()
    for day in sorted(by_day):
        if layout.ensure(20, "Events per day (continued)"):
            header()
        counts = by_day[day]
        page = layout.page
        label = day
        if day != "Unknown date":
            label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
        breakdown = ", ".join(f"{_label(t)} {c}" for t, c in sorted(counts.items(), key=lambda kv: _type_order(kv[0])))
        page.text(MARGIN + 10, layout.y + 13, label, 9)
        page.text(MARGIN + 130, layout.y + 13, str(sum(counts.values())), 9, True, align="right")
        page.text(MARGIN + 150, layout.y + 13, fit_text(breakdown, 9, CONTENT_W - 160), 9, color=GRAY)
        page.line(MARGIN, layout.y + 19, PAGE_W - MARGIN, layout.y + 19)
        layout.y += 19
    layout.y += 14


def _draw_trips(layout: _Layout, trips: dict | None):
    trip_list = (trips or {}).get("trips") or []
    if not trip_list:
        return
    totals = (trips or {}).get("totals") or {}

    def hours(minutes):
        value = _to_float(minutes) or 0.0
        return f"{int(value // 60)} h {int(value % 60):02d} min"

    rows = [
        ("GPS trips recorded", str(len(trip_list))),
        ("Total distance", f"{sum(_to_float(t.get('total_distance_km')) or 0.0 for t in trip_list):,.1f} km"),
        ("Driving time", hours(totals.get("moving_duration_minutes"))),
        ("Idle time (engine on, not moving)", hours(totals.get("idle_duration_minutes"))),
        ("Long stops", str(int(_to_float(totals.get("long_idle_stop_count")) or 0))),
    ]
    layout.ensure(40 + 20 * len(rows))
    layout.heading("Trips (all GPS trips stored on the device)")
    page = layout.page
    for key, value in rows:
        page.text(MARGIN + 10, layout.y + 13, key, 9, color=GRAY)
        page.text(MARGIN + 300, layout.y + 13, value, 10, True, align="right")
        page.line(MARGIN, layout.y + 19, MARGIN + 310, layout.y + 19)
        layout.y += 19
    layout.y += 14


def _caption(event: dict) -> tuple[str, str]:
    start = _to_float(event.get("start"))
    end = _to_float(event.get("end"))
    line1 = _fmt_time(start, "%d %b %Y, %H:%M:%S") if start else "Time unknown"
    if start and end and 0 < end - start < 3600:
        line1 += f"  -  {end - start:.0f} s"
    speed = str(event.get("speed") or "").strip()
    line2 = f"Speed {speed} km/h" if speed else "Speed unknown"
    lat, lon = _to_float(event.get("lat")), _to_float(event.get("long"))
    if lat and lon:
        line2 += f"  -  {lat:.5f}, {lon:.5f}"
    return line1, line2


def _draw_snapshots(layout: _Layout, events: list[dict], per_type: int):
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_type[e["type"]].append(e)

    cell_w = (CONTENT_W - 16) / 2
    img_h = cell_w * 3 / 4
    cell_h = img_h + 40
    for event_type in sorted(by_type, key=_type_order):
        type_events = sorted(by_type[event_type], key=lambda e: _to_float(e.get("start")) or 0.0)
        loaded = []
        for event in pick_evenly(type_events, per_type):
            folder = event.get("folder")
            for candidate in _snapshot_candidates(Path(folder)) if folder else []:
                try:
                    index, width, height = layout.doc.add_jpeg(candidate.read_bytes())
                except (OSError, ValueError):
                    continue
                loaded.append((index, width, height, event))
                break
        if not loaded:
            continue

        label = _label(event_type)
        for chunk_start in range(0, len(loaded), 4):
            title = f"{label} - sample snapshots" + (" (continued)" if chunk_start else "")
            page = layout.new_page(title)
            if not chunk_start:
                page.text(MARGIN, layout.y, f"{len(loaded)} of {len(type_events)} {label} events shown, "
                                           "spread evenly from first to last.", 9, color=GRAY)
                layout.y += 14
            for i, (index, width, height, event) in enumerate(loaded[chunk_start:chunk_start + 4]):
                x = MARGIN + (i % 2) * (cell_w + 16)
                y = layout.y + (i // 2) * (cell_h + 12)
                scale = min(cell_w / width, img_h / height)
                draw_w, draw_h = width * scale, height * scale
                page.rect(x, y, cell_w, img_h, fill=(0.93, 0.94, 0.96))
                page.image(index, x + (cell_w - draw_w) / 2, y + (img_h - draw_h) / 2, draw_w, draw_h)
                page.rect(x, y, cell_w, img_h, stroke=BORDER)
                line1, line2 = _caption(event)
                page.rect(x, y + img_h + 6, 4, 26, fill=EVENT_COLORS.get(event_type, DEFAULT_COLOR))
                page.text(x + 10, y + img_h + 17, fit_text(line1, 9, cell_w - 12, True), 9, True)
                page.text(x + 10, y + img_h + 30, fit_text(line2, 8, cell_w - 12), 8, color=GRAY)


def build_vehicle_report(vehicle: dict, events: list[dict], trips: dict | None = None,
                         generated: float | None = None, images_per_type: int = IMAGES_PER_TYPE) -> bytes:
    """events: dicts with type, start, end (unix), speed, lat, long, folder (event dir with frame_*.jpg)."""
    generated = generated if generated is not None else datetime.now().timestamp()
    number = vehicle.get("vehicle_number") or "Unregistered vehicle"
    doc = PdfDocument(title=f"Vehicle Safety Report - {number}")
    layout = _Layout(doc)

    _draw_cover(layout, vehicle, events, trips, generated)
    _draw_type_table(layout, events)
    _draw_hour_chart(layout, events)
    _draw_daily_table(layout, events)
    _draw_trips(layout, trips)
    _draw_snapshots(layout, events, images_per_type)

    footer = f"Vehicle {number}  |  Generated {_fmt_time(generated)}  |  BlinkSmart driver monitoring (offline)"
    total = len(doc.pages)
    for i, page in enumerate(doc.pages, 1):
        page.line(MARGIN, FOOTER_Y - 10, PAGE_W - MARGIN, FOOTER_Y - 10)
        page.text(MARGIN, FOOTER_Y + 2, fit_text(footer, 8, CONTENT_W - 70), 8, color=GRAY)
        page.text(PAGE_W - MARGIN, FOOTER_Y + 2, f"Page {i} of {total}", 8, color=GRAY, align="right")
    return doc.to_bytes()


def write_vehicle_report(path: Path, vehicle: dict, events: list[dict], trips: dict | None = None,
                         generated: float | None = None) -> None:
    data = build_vehicle_report(vehicle, events, trips, generated)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
