#!/usr/bin/env python3
"""
Pull the current draft beer list from Toast POS and generate:
  - draft_list.pdf   (landscape, for the WordPress site)
  - draft_list.html  (self-contained, auto-refreshing, for the TV monitor)

Usage:
  python generate_draft_list.py                  # live: pulls from Toast
  python generate_draft_list.py --sample         # offline: uses sample_menu.json
  python generate_draft_list.py --output-dir out # writes outputs to ./out

Configuration (env vars or .env file in the same directory):
  TOAST_CLIENT_ID            Standard API Access client id
  TOAST_CLIENT_SECRET        Standard API Access client secret
  TOAST_RESTAURANT_GUID      Your restaurant GUID
  TOAST_HOST                 Default https://ws-api.toasttab.com
  DRAFT_GROUP_NAME           Default "Draft Beer" (must match the group in Toast)
  BAR_NAME                   Header on the PDF/HTML, default "On Tap"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------- Config ----------------------------------------------------------

DEFAULTS = {
    "TOAST_HOST": "https://ws-api.toasttab.com",
    "DRAFT_GROUP_NAME": "Draft Beer",
    # If set, only look for the group inside this parent menu (case-insensitive).
    # Useful when more than one menu in the restaurant contains a "Draft Beer"
    # group — e.g., Chillicothe has it under both "Beer" and elsewhere.
    "DRAFT_MENU_NAME":  "",
    "BAR_NAME": "On Tap",
    # If set (e.g., "24"), always render this many tap slots even when some are
    # missing from Toast — empty slots show the tap number with no beer info, so
    # the 3×8 grid layout stays consistent.
    "EXPECTED_TAP_COUNT": "",
}


def load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't add a dependency for one feature."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def cfg(key: str) -> str:
    return os.environ.get(key, DEFAULTS.get(key, ""))


# ---------- Data model ------------------------------------------------------


@dataclass
class Beer:
    name: str
    price: float
    brewery: str = ""
    style: str = ""
    abv: str = ""
    tap: str = ""
    glass: str = ""
    note: str = ""

    @property
    def tap_sort_key(self) -> tuple:
        # Tap "3" -> (0, 3); missing -> (1, 0) so untapped go last.
        try:
            return (0, int(re.sub(r"\D", "", self.tap)))
        except (ValueError, TypeError):
            return (1, 0)


# ---------- Description parser ---------------------------------------------

_KEY_ALIASES = {
    "brewery": "brewery",
    "brewer": "brewery",
    "style": "style",
    "abv": "abv",
    "tap": "tap",
    "tap#": "tap",
    "glass": "glass",
    "size": "glass",
}

# Matches a known key followed by a colon, anywhere in a chunk.
# Used to detect "Crisp finish. Brewery: Allagash" style chunks where a
# free-text note runs straight into a key:value pair without a `|` between them.
_KEY_BOUNDARY_RE = re.compile(
    r"(?i)(?:^|[\s.;,])(brewery|brewer|style|abv|tap#?|glass|size)\s*:"
)


def parse_description(text: str) -> dict:
    """Pull Brewery/Style/ABV/Tap/Glass out of a pipe-separated description.

    Free-text tasting notes can appear anywhere — before the first key:value,
    or in their own chunk between key:value pairs. They're collected into `note`.
    """
    if not text:
        return {}
    chunks = [c.strip() for c in text.split("|") if c.strip()]
    parsed: dict = {"note": ""}
    note_parts: list[str] = []
    for chunk in chunks:
        # Find where (if anywhere) a known-key marker starts inside this chunk.
        m = _KEY_BOUNDARY_RE.search(chunk)
        if not m:
            note_parts.append(chunk)
            continue
        # Anything before the key marker is a note prefix.
        prefix = chunk[: m.start()].strip(" .;,")
        if prefix:
            note_parts.append(prefix)
        # Parse the key:value portion (skip past leading punctuation in the match).
        kv = chunk[m.start():].lstrip(" .;,")
        key, _, val = kv.partition(":")
        canonical = _KEY_ALIASES.get(key.strip().lower())
        if canonical:
            parsed[canonical] = val.strip()
        else:
            note_parts.append(kv)
    parsed["note"] = " ".join(note_parts).strip()
    return parsed


# ---------- Toast API client -----------------------------------------------


def fetch_menu_from_toast() -> dict:
    """Authenticate against Toast and fetch the full menu JSON.

    Lazy-imports requests so --sample mode works without it installed.
    """
    import requests  # type: ignore

    client_id = cfg("TOAST_CLIENT_ID")
    client_secret = cfg("TOAST_CLIENT_SECRET")
    restaurant_guid = cfg("TOAST_RESTAURANT_GUID")
    host = cfg("TOAST_HOST")

    missing = [
        n for n, v in [
            ("TOAST_CLIENT_ID", client_id),
            ("TOAST_CLIENT_SECRET", client_secret),
            ("TOAST_RESTAURANT_GUID", restaurant_guid),
        ] if not v
    ]
    if missing:
        sys.exit(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your credentials, "
            "or run with --sample to use the bundled sample data."
        )

    auth = requests.post(
        f"{host}/authentication/v1/authentication/login",
        json={
            "clientId": client_id,
            "clientSecret": client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        },
        timeout=15,
    )
    auth.raise_for_status()
    token = auth.json()["token"]["accessToken"]

    menu_resp = requests.get(
        f"{host}/menus/v2/menus",
        headers={
            "Authorization": f"Bearer {token}",
            "Toast-Restaurant-External-ID": restaurant_guid,
        },
        timeout=20,
    )
    menu_resp.raise_for_status()
    return menu_resp.json()


# ---------- Menu -> Beer list ----------------------------------------------


_NAME_TAP_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[\.\)\-:]\s*(.*)$")
_NAME_ABV_SUFFIX_RE = re.compile(r"^(.*?)\s+(\d+(?:\.\d+)?)\s*%\s*(?:ABV)?\s*$", re.IGNORECASE)


def split_tap_from_name(raw_name: str) -> tuple[str, str]:
    """If the item name starts with a number-and-separator (e.g. '1. American Lager',
    '12) Doom Pedal'), return (tap_number, clean_name).
    Otherwise return ('', raw_name).
    """
    if not raw_name:
        return "", ""
    m = _NAME_TAP_PREFIX_RE.match(raw_name)
    if m:
        return m.group(1), m.group(2).strip()
    return "", raw_name.strip()


def split_abv_from_name(name: str) -> tuple[str, str]:
    """If name ends with an ABV (e.g., 'Apple Light 4.2%', 'Atomic Inn 6.0% ABV'),
    return (abv_with_percent, clean_name). Otherwise ('', name).
    """
    if not name:
        return "", name
    m = _NAME_ABV_SUFFIX_RE.match(name)
    if m:
        clean = m.group(1).strip()
        abv = f"{m.group(2)}%"
        return abv, clean
    return "", name


def extract_drafts(menu_payload: dict, group_name: str,
                   menu_name: str | None = None) -> list[Beer]:
    """Find a group by name and return the beers inside it.

    If `menu_name` is provided, only groups inside that parent menu are
    considered — useful when multiple menus contain a group with the same
    name (e.g., a "Draft Beer" group both in the "Beer" menu and in the
    "Online Menu and Takeout" menu's "32 oz Crowlers of Draft Beer").
    """
    target_group = group_name.strip().lower()
    target_menu = menu_name.strip().lower() if menu_name else None
    beers: list[Beer] = []
    for menu in menu_payload.get("menus", []):
        if target_menu and menu.get("name", "").strip().lower() != target_menu:
            continue
        for group in menu.get("menuGroups", []):
            if group.get("name", "").strip().lower() != target_group:
                continue
            for item in group.get("menuItems", []):
                meta = parse_description(item.get("description", ""))
                # Tap number resolution priority:
                #   1. explicit "Tap: N" in description
                #   2. leading "N. " (or "N) ", "N - ") in the item name
                # ABV resolution priority:
                #   1. explicit "ABV: X%" in description
                #   2. trailing "X.Y%" or "X.Y% ABV" at end of item name
                raw_name = item.get("name", "Untitled")
                name_tap, after_tap = split_tap_from_name(raw_name)
                name_abv, clean_name = split_abv_from_name(after_tap)
                tap = meta.get("tap") or name_tap
                abv = meta.get("abv") or name_abv
                beers.append(
                    Beer(
                        name=clean_name,
                        price=float(item.get("price", 0) or 0),
                        brewery=meta.get("brewery", ""),
                        style=meta.get("style", ""),
                        abv=abv,
                        tap=tap,
                        glass=meta.get("glass", ""),
                        note=meta.get("note", ""),
                    )
                )
    beers.sort(key=lambda b: b.tap_sort_key)
    return beers


def fill_taps(beers: list[Beer], total_slots: int) -> list[Beer]:
    """Pad the beer list with empty slots for any missing tap numbers in [1, total_slots].

    Example: if total_slots=24 and beers covers taps 1-6, 8-24, this returns 24
    Beer objects with an empty placeholder for tap 7. The empty placeholder has
    no name/style/abv/price — the renderer shows just the tap number in muted
    styling for empty slots.
    """
    by_tap: dict[int, Beer] = {}
    extras: list[Beer] = []  # beers without a usable numeric tap
    for b in beers:
        try:
            n = int(re.sub(r"\D", "", b.tap or ""))
            if n and n not in by_tap:
                by_tap[n] = b
            else:
                extras.append(b)
        except (ValueError, TypeError):
            extras.append(b)

    filled: list[Beer] = []
    for n in range(1, total_slots + 1):
        if n in by_tap:
            filled.append(by_tap[n])
        else:
            filled.append(Beer(name="", price=0.0, tap=str(n)))
    # Anything without a tap number in [1, total_slots] is silently dropped.
    # Toast may contain cans/upcoming/removed items mixed into the same menu
    # group — we only display the numbered tap slots the user explicitly set.
    return filled


# ---------- PDF rendering ---------------------------------------------------


# ---------- Brand loading --------------------------------------------------


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def load_brand(here: Path) -> dict:
    """Load brand config; merge user file over hard-coded defaults so missing keys are fine."""
    defaults = {
        "colors": {
            "header_bar":    "#C66331",
            "accent_orange": "#C66331",
            "accent_sage":   "#93B0A0",
            "background":    "#F2EAD5",
            "text_dark":     "#1B3D38",
            "text_light":    "#F5EDDD",
        },
        "header": {
            "title":      "Draft Beer",
            "left_badge": "$7\nPints",
            "right_text": "$10 32OZ\nCROWLERS TO-GO",
            "empty_slot_text": "New brew coming soon",
        },
        "fonts": {
            "title_font_file":   None, "title_font_name":   "Times-Bold",
            "body_font_file":    None, "body_font_name":    "Helvetica",
            "body_bold_file":    None, "body_bold_name":    "Helvetica-Bold",
            "badge_label_file":  None, "badge_label_name":  "Helvetica",
        },
        "logo": {"path": None, "overlay_text": False},
    }
    bf = here / "brand.json"
    if bf.exists():
        user = json.loads(bf.read_text())
        for section, vals in user.items():
            if section.startswith("_"):
                continue
            if isinstance(vals, dict):
                defaults.setdefault(section, {}).update({
                    k: v for k, v in vals.items() if not k.startswith("_")
                })
    return defaults


def register_brand_fonts(brand: dict, here: Path) -> dict:
    """Register any TTF/OTF files referenced in brand.json with reportlab.

    On success, the *_name field is replaced with the registered internal name.
    On failure (e.g. CFF/PostScript outline OTF that reportlab can't read), the
    *_name field is reset to a safe built-in font so the PDF still renders.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    builtin_fallback = {
        "title_font":  "Times-Bold",
        "body_font":   "Helvetica",
        "body_bold":   "Helvetica-Bold",
        "badge_label": "Helvetica",
    }
    fonts_dir = here / "brand" / "fonts"
    for slot in ("title_font", "body_font", "body_bold", "badge_label"):
        rel = brand["fonts"].get(f"{slot}_file")
        if not rel:
            continue
        font_path = fonts_dir / rel
        if not font_path.exists():
            print(f"  ! font file missing: {font_path}, falling back to built-in",
                  file=sys.stderr)
            brand["fonts"][f"{slot}_name"] = builtin_fallback[slot]
            continue
        internal_name = f"brand-{slot}"
        try:
            pdfmetrics.registerFont(TTFont(internal_name, str(font_path)))
            brand["fonts"][f"{slot}_name"] = internal_name
        except Exception as e:
            print(f"  ! could not register font {font_path.name}: {e}", file=sys.stderr)
            print(f"    Hint: if it's a CFF .otf, run: python convert_otf.py "
                  f"brand/fonts/{font_path.name}", file=sys.stderr)
            brand["fonts"][f"{slot}_name"] = builtin_fallback[slot]
    return brand


# ---------- PDF rendering ---------------------------------------------------


def render_pdf(beers: Iterable[Beer], out_path: Path, bar_name: str,
               brand: dict | None = None) -> None:
    from reportlab.lib.pagesizes import landscape, LETTER
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    if brand is None:
        brand = load_brand(Path(__file__).resolve().parent)

    C = {k: _hex_to_rgb(v) for k, v in brand["colors"].items()}
    F = brand["fonts"]
    H = brand["header"]

    beers = list(beers)
    page_w, page_h = landscape(LETTER)  # 11 x 8.5 in
    c = canvas.Canvas(str(out_path), pagesize=landscape(LETTER))

    # ---- Cream background covering the whole page
    c.setFillColorRGB(*C["background"])
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    # ---- Orange header bar
    bar_h = 1.55 * inch
    c.setFillColorRGB(*C["header_bar"])
    c.rect(0, page_h - bar_h, page_w, bar_h, fill=1, stroke=0)

    # ---- Header: left badge ($7 Pints or logo)
    badge_cx = 1.2 * inch
    badge_cy = page_h - bar_h / 2
    logo_cfg = brand.get("logo") or {}
    logo_path_str = logo_cfg.get("path")
    overlay_text = bool(logo_cfg.get("overlay_text"))
    logo_path = Path(__file__).resolve().parent / logo_path_str if logo_path_str else None
    drew_logo = False
    if logo_path and logo_path.exists():
        try:
            img = ImageReader(str(logo_path))
            iw, ih = img.getSize()
            target_h = bar_h * 0.92
            target_w = iw * (target_h / ih)
            c.drawImage(img, badge_cx - target_w / 2, badge_cy - target_h / 2,
                        target_w, target_h, mask="auto")
            drew_logo = True
        except Exception as e:
            print(f"  ! couldn't draw logo {logo_path}: {e}", file=sys.stderr)
            drew_logo = False

    # Decide whether to draw the badge text
    # - no logo set         → draw flat sage circle + text
    # - logo set, no overlay → image stands alone, no text
    # - logo set + overlay   → image as background + text on top
    draw_text = (not drew_logo) or overlay_text

    if not drew_logo:
        # Fallback: flat sage circle
        c.setFillColorRGB(*C["accent_sage"])
        c.circle(badge_cx, badge_cy, 0.6 * inch, fill=1, stroke=0)

    if draw_text:
        lines = (H.get("left_badge") or "").split("\n")
        c.setFillColorRGB(*C["text_light"])
        if len(lines) >= 1:
            # First line ($7) uses the chunky serif headline font
            c.setFont(F["title_font_name"], 28)
            c.drawCentredString(badge_cx, badge_cy + 0.02 * inch, lines[0])
        if len(lines) >= 2:
            # Second line (Pints) uses the script font for a decorative look
            c.setFont(F["badge_label_name"], 18)
            c.drawCentredString(badge_cx, badge_cy - 0.30 * inch, lines[1])

    # ---- Header: title (centered)
    c.setFillColorRGB(*C["text_light"])
    c.setFont(F["title_font_name"], 56)
    title = H.get("title") or "Draft Beer"
    c.drawCentredString(page_w / 2, page_h - bar_h / 2 - 0.18 * inch, title)

    # ---- Header: right callout (center-aligned within the right block)
    right_lines = (H.get("right_text") or "").split("\n")
    c.setFillColorRGB(*C["text_light"])
    c.setFont(F["body_bold_name"], 13)
    # Center axis sits in the right portion of the header bar; tuned so the
    # multi-line callout reads as a single centered block.
    right_cx = page_w - 1.4 * inch
    base_y = page_h - bar_h / 2 + 0.05 * inch
    for i, ln in enumerate(right_lines):
        c.drawCentredString(right_cx, base_y - i * 0.22 * inch, ln)

    # ---- Body grid: 3 cols if >16, 2 cols if 9-16, 1 col if ≤8
    if len(beers) > 16:
        n_cols = 3
    elif len(beers) > 8:
        n_cols = 2
    else:
        n_cols = 1

    margin = 0.45 * inch
    gutter = 0.4 * inch
    col_w = (page_w - 2 * margin - gutter * (n_cols - 1)) / n_cols
    top_y = page_h - bar_h - 0.4 * inch
    bot_y = 0.35 * inch
    available_h = top_y - bot_y
    rows_per_col = max(1, (len(beers) + n_cols - 1) // n_cols)
    row_h = min(0.85 * inch, available_h / rows_per_col)

    def render_column(beers_subset: list[Beer], x0: float, y0: float) -> None:
        # Inside a column:
        #   [big tap#]  [BEER NAME ALL CAPS]                  [ABV%]
        #               [Style/note in dark]
        #               <hairline>
        tap_x   = x0
        name_x  = x0 + 0.75 * inch     # leaves room for 2-digit big tap number
        abv_r   = x0 + col_w           # ABV right-aligned at column edge
        # Reserve a band on the right for ABV so the name never collides with it.
        abv_band = 0.6 * inch
        name_max = abv_r - abv_band - name_x

        # Use reportlab's actual stringWidth measurement.
        # Strategy: try the preferred font size first, step down to smaller
        # sizes if the text doesn't fit. Only truncate as a last resort.
        from reportlab.pdfbase import pdfmetrics

        def measure(text: str, font_name: str, font_size: float) -> float:
            try:
                return pdfmetrics.stringWidth(text, font_name, font_size)
            except Exception:
                return len(text) * font_size * 0.5  # rough fallback

        def fit_shrink(text: str, max_width: float, font_name: str,
                       sizes_to_try: list[float]) -> tuple[str, float]:
            """Return (text, font_size). Picks the largest size that fits the full text.
            If even the smallest size doesn't fit, truncates with ellipsis at that size.
            """
            if not text:
                return text, sizes_to_try[0]
            for sz in sizes_to_try:
                if measure(text, font_name, sz) <= max_width:
                    return text, sz
            # Smallest size still doesn't fit — truncate at smallest
            sz = sizes_to_try[-1]
            ell = "…"
            ell_w = measure(ell, font_name, sz)
            lo, hi, best = 1, len(text), 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if measure(text[:mid], font_name, sz) + ell_w <= max_width:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return text[:best].rstrip() + ell, sz

        y = y0
        # Sizes the beer-name font is allowed to shrink to, in order.
        name_sizes = [13, 12, 11, 10]
        sub_sizes  = [11, 10, 9]

        for beer in beers_subset:
            empty_slot = not beer.name  # tap number known, no beer info

            # Big tap number — always sage; muted for empty slots
            if empty_slot:
                # Lighter sage with reduced opacity feel
                r, g, b = C["accent_sage"]
                c.setFillColorRGB(
                    r + (1 - r) * 0.55,
                    g + (1 - g) * 0.55,
                    b + (1 - b) * 0.55,
                )
            else:
                c.setFillColorRGB(*C["accent_sage"])
            c.setFont(F["title_font_name"], 32)
            c.drawString(tap_x, y - 0.30 * inch, beer.tap or "—")

            if empty_slot:
                # "New brew coming soon" placeholder. Uses regular (not black) weight
                # and original case to visually distinguish from real beer names and
                # to fit comfortably without truncation.
                msg = (H.get("empty_slot_text") or "").strip()
                if msg:
                    r, g, b = C["accent_orange"]
                    c.setFillColorRGB(
                        r + (1 - r) * 0.45,
                        g + (1 - g) * 0.45,
                        b + (1 - b) * 0.45,
                    )
                    msg_text, msg_size = fit_shrink(
                        msg, name_max,
                        F["body_font_name"], [12, 11, 10, 9, 8],
                    )
                    c.setFont(F["body_font_name"], msg_size)
                    c.drawString(name_x, y - 0.18 * inch, msg_text)
            else:
                # Beer name — orange, ALL CAPS, bold. Shrink before truncating.
                c.setFillColorRGB(*C["accent_orange"])
                name_text, name_size = fit_shrink(
                    (beer.name or "").upper(), name_max,
                    F["body_bold_name"], name_sizes,
                )
                c.setFont(F["body_bold_name"], name_size)
                c.drawString(name_x, y - 0.16 * inch, name_text)

                # Style / note — dark text underneath
                substyle = beer.style or ""
                if beer.note:
                    substyle = f"{substyle} · {beer.note}" if substyle else beer.note
                if substyle:
                    c.setFillColorRGB(*C["text_dark"])
                    sub_text, sub_size = fit_shrink(
                        substyle, abv_r - name_x,
                        F["body_font_name"], sub_sizes,
                    )
                    c.setFont(F["body_font_name"], sub_size)
                    c.drawString(name_x, y - 0.36 * inch, sub_text)

                # ABV — right-aligned, dark
                if beer.abv:
                    c.setFillColorRGB(*C["text_dark"])
                    c.setFont(F["body_font_name"], 13)
                    c.drawRightString(abv_r, y - 0.20 * inch, beer.abv)

            # Hairline separator — always drawn so the grid stays consistent
            sep_y = y - row_h + 0.10 * inch
            c.setStrokeColorRGB(*C["text_dark"])
            c.setLineWidth(0.4)
            c.setDash(1, 0)
            c.line(name_x, sep_y, abv_r, sep_y)

            y -= row_h

    if not beers:
        c.setFillColorRGB(*C["text_dark"])
        c.setFont(F["body_font_name"], 14)
        c.drawString(margin, top_y - 0.4 * inch,
                     "No drafts currently configured in Toast.")
    else:
        per_col = rows_per_col
        for col_i in range(n_cols):
            subset = beers[col_i * per_col : (col_i + 1) * per_col]
            x0 = margin + col_i * (col_w + gutter)
            render_column(subset, x0, top_y)

    c.save()


# ---------- HTML rendering --------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{bar_name} — Draft Beer</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<script>
  // Reload the page every 60s with a cache-busting query string so the TV
  // always shows the latest published version, never a stale cached copy.
  setTimeout(function() {{
    var u = new URL(window.location.href);
    u.searchParams.set('t', Date.now());
    window.location.replace(u.toString());
  }}, 60000);
</script>
<style>
  {font_face_rules}
  :root {{
    --header-bar: {c_header};
    --orange: {c_orange};
    --sage: {c_sage};
    --bg: {c_bg};
    --text: {c_text};
    --light: {c_light};
    --lime: {c_lime};
    --tap-size: {tap_size}vh;
    --name-size: {name_size}vh;
    --sub-size: {sub_size}vh;
    --abv-size: {abv_size}vh;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: '{body_font_name}', 'Helvetica Neue', Helvetica, Arial, sans-serif;
    height: 100%; overflow: hidden;
  }}
  .header-bar {{
    background: var(--header-bar);
    height: 16vh;
    padding: 0 3vw;
    display: grid;
    grid-template-columns: 14vw 1fr 22vw;
    align-items: center;
    color: var(--light);
    overflow: visible;  /* let the badge hang past the bottom edge */
    position: relative;
    z-index: 2;
  }}
  .badge {{
    /* Badge is taller than the orange bar so it "breaks out" past both edges,
       like the designer's spec — top sparkle pokes above the bar into the white
       margin, sage bottom hangs into the body below the bar. */
    width: 22vh; height: 22vh;
    {badge_bg_css}
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    color: var(--light);
    line-height: 1;
    /* Anchor the sage circle (which starts 13.2% into the PNG) to the top of
       the bar. PNG sits with its top edge 3vh above the page, so the top sparkle
       extends off the top of the viewport — matching the designer's spec where
       the sparkle floats above the orange bar. */
    transform: translateY(0%);
    position: relative;
    z-index: 3;
  }}
  .badge .price {{ font-family: '{title_font_name}', Georgia, serif; font-size: 7vh; }}
  .badge .label {{ font-family: '{badge_label_font_name}', cursive; font-size: 4vh; margin-top: 0.4vh; letter-spacing: 0.02em; }}
  .header-bar .title {{
    font-family: '{title_font_name}', Georgia, serif;
    font-size: 11vh;
    text-align: center;
    line-height: 1;
  }}
  .header-bar .right {{
    text-align: center;
    font-weight: 700;
    font-size: 2.4vh;
    line-height: 1.2;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .grid {{
    /* Top padding clears the badge that hangs past the orange bar */
    padding: 5vh 3vw 1vh 3vw;
    height: 82vh;
    display: grid;
    grid-template-columns: {grid_cols};
    column-gap: 3vw;
    align-content: start;
    overflow: hidden;
  }}
  ul.beers {{ list-style: none; margin: 0; padding: 0; }}
  ul.beers li {{
    display: grid;
    /* Tap column scales with the tap font size so 2-digit Goudy numbers fit
       without overlapping the beer name. Tighter gap and tap column gives the
       beer name more horizontal room. */
    grid-template-columns: calc(var(--tap-size) * 1.65) minmax(0, 1fr) auto;
    column-gap: 0.5vw;
    align-items: center;
    padding: 0.35vh 0;
    border-bottom: 1px solid color-mix(in srgb, var(--lime) 45%, transparent);
    min-width: 0;  /* allow ellipsis to work inside grid */
  }}
  ul.beers li .beer {{
    min-width: 0;       /* enable text-overflow inside grid cell */
    overflow: hidden;
  }}
  .tap {{
    font-family: '{title_font_name}', Georgia, serif;
    font-size: var(--tap-size);
    line-height: 0.9;
    text-align: left;
    align-self: center;
    color: var(--sage);
  }}
  .tap.empty {{ opacity: 0.45; }}
  /* Placeholder for empty taps — small, italic, faded so it reads as
     "this tap is empty" instead of competing visually with real beer names. */
  .beer .name.empty {{
    text-transform: none;
    font-weight: 400;
    font-style: italic;
    font-size: var(--sub-size);
    color: var(--orange);
    opacity: 0.45;
    letter-spacing: 0;
  }}
  .beer .name {{
    color: var(--orange);
    font-weight: 800;
    font-size: var(--name-size);
    text-transform: uppercase;
    letter-spacing: 0.02em;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .beer .substyle {{
    color: var(--text);
    font-size: var(--sub-size);
    margin-top: 0.2vh;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .abv {{
    color: var(--text);
    font-size: var(--abv-size);
    align-self: center;
    text-align: right;
    font-weight: 600;
  }}
  .empty {{
    color: var(--text); font-style: italic; padding: 6vh; text-align: center; font-size: 3vh;
  }}
</style>
</head>
<body>
<div class="header-bar">
  <div class="badge">
    <span class="price">{badge_price}</span>
    <span class="label">{badge_label}</span>
  </div>
  <div class="title">{title}</div>
  <div class="right">{right_html}</div>
</div>
{body}
</body>
</html>
"""


def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_beer_li(b: "Beer", empty_slot_text: str = "") -> str:
    empty_slot = not b.name
    if empty_slot:
        tap_html  = f'<span class="tap empty">{html_escape(b.tap or "—")}</span>'
        name_html = f'<span class="name empty">{html_escape(empty_slot_text)}</span>' if empty_slot_text else ""
        return (
            '<li class="empty-row">'
            f'{tap_html}'
            f'<span class="beer">{name_html}</span>'
            '<span class="abv"></span>'
            '</li>'
        )
    substyle_parts = [s for s in (b.style, b.note) if s]
    substyle = " · ".join(substyle_parts)
    substyle_html = (
        f'<span class="substyle">{html_escape(substyle)}</span>' if substyle else ""
    )
    abv_html = f'<span class="abv">{html_escape(b.abv)}</span>' if b.abv else '<span class="abv"></span>'
    return (
        '<li>'
        f'<span class="tap">{html_escape(b.tap or "—")}</span>'
        f'<span class="beer"><span class="name">{html_escape(b.name)}</span>{substyle_html}</span>'
        f'{abv_html}'
        '</li>'
    )


def _column_html_with_empty(beers_subset: list["Beer"], empty_slot_text: str) -> str:
    rows = "".join(_render_beer_li(b, empty_slot_text) for b in beers_subset)
    return f'<div class="col"><ul class="beers">{rows}</ul></div>'


def _column_html(beers_subset: list["Beer"]) -> str:
    rows = "".join(_render_beer_li(b) for b in beers_subset)
    return f'<div class="col"><ul class="beers">{rows}</ul></div>'


def _build_font_face_rules(brand: dict, here: Path) -> str:
    """Embed brand fonts as base64 @font-face rules so the HTML is fully self-contained.

    OTF / TTF only. If a font file is missing, that face is just skipped — CSS will use
    the fallback in the font stack.
    """
    import base64
    rules = []
    F = brand["fonts"]
    for slot in ("title_font", "body_font", "body_bold", "badge_label"):
        rel = F.get(f"{slot}_file")
        family = F.get(f"{slot}_name")
        if not rel or not family:
            continue
        path = here / "brand" / "fonts" / rel
        if not path.exists():
            continue
        ext = path.suffix.lower().lstrip(".")
        fmt = "opentype" if ext == "otf" else "truetype"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f"@font-face {{ font-family: '{family}'; "
            f"src: url(data:font/{ext};base64,{b64}) format('{fmt}'); "
            f"font-display: swap; }}"
        )
    return "\n  ".join(rules)


def render_html(beers: Iterable["Beer"], out_path: Path, bar_name: str,
                brand: dict | None = None) -> None:
    beers = list(beers)

    if brand is None:
        brand = load_brand(Path(__file__).resolve().parent)

    # Choose column count to match PDF
    if len(beers) > 16:
        n_cols = 3
    elif len(beers) > 8:
        n_cols = 2
    else:
        n_cols = 1
    rows_per_col = max(1, (len(beers) + n_cols - 1) // n_cols)

    # Auto-scale typography for legibility on a 1080p TV.
    # Sizes tuned so long beer names (Coast to Coast, Keep on Truckin',
    # East Coast Hazy IPA, etc.) fit at fullscreen 16:9 without truncation.
    per_row = 82 / rows_per_col
    tap_size  = round(min(5.4, per_row * 0.52), 1)
    name_size = round(min(3.1, per_row * 0.30), 1)
    sub_size  = round(min(2.2, per_row * 0.23), 1)
    abv_size  = round(min(2.8, per_row * 0.30), 1)

    H = brand["header"]
    badge_lines = (H.get("left_badge") or "$\nPints").split("\n")
    badge_price = badge_lines[0] if badge_lines else ""
    badge_label = badge_lines[1] if len(badge_lines) > 1 else ""
    right_html = "<br>".join(html_escape(ln) for ln in (H.get("right_text") or "").split("\n"))
    title = html_escape(H.get("title") or "Draft Beer")

    empty_slot_text = (H.get("empty_slot_text") or "").strip()
    if not beers:
        body = '<div class="empty">No drafts currently configured in Toast.</div>'
        grid_cols = "1fr"
    else:
        per_col = rows_per_col
        cols_html = "".join(
            _column_html_with_empty(beers[i * per_col : (i + 1) * per_col], empty_slot_text)
            for i in range(n_cols)
        )
        body = f'<div class="grid">{cols_html}</div>'
        grid_cols = " ".join(["1fr"] * n_cols)

    # Use registered TTF names if user supplied font files; otherwise serif/sans fallbacks.
    F = brand["fonts"]
    title_font_name = F.get("title_font_name") or "Times-Bold"
    body_font_name = F.get("body_font_name") or "Helvetica"
    badge_label_font_name = F.get("badge_label_name") or body_font_name

    here = Path(__file__).resolve().parent
    font_face_rules = _build_font_face_rules(brand, here)

    # Badge background: either embedded image (base64) or a flat sage circle.
    badge_bg_css = "background: var(--sage); border-radius: 50%;"
    logo_cfg = brand.get("logo") or {}
    logo_path_str = logo_cfg.get("path")
    if logo_path_str:
        logo_full = here / logo_path_str
        if logo_full.exists():
            import base64
            ext = logo_full.suffix.lower().lstrip(".") or "png"
            mime = "image/png" if ext == "png" else f"image/{ext}"
            b64 = base64.b64encode(logo_full.read_bytes()).decode("ascii")
            badge_bg_css = (
                f"background: url('data:{mime};base64,{b64}') "
                "no-repeat center/contain;"
            )

    out_path.write_text(HTML_TEMPLATE.format(
        font_face_rules=font_face_rules,
        badge_bg_css=badge_bg_css,
        bar_name=html_escape(bar_name),
        c_header=brand["colors"]["header_bar"],
        c_orange=brand["colors"]["accent_orange"],
        c_sage=brand["colors"]["accent_sage"],
        c_bg=brand["colors"]["background"],
        c_text=brand["colors"]["text_dark"],
        c_light=brand["colors"]["text_light"],
        c_lime=brand["colors"].get("accent_lime", brand["colors"]["accent_sage"]),
        title_font_name=title_font_name,
        body_font_name=body_font_name,
        badge_label_font_name=badge_label_font_name,
        title=title,
        badge_price=html_escape(badge_price),
        badge_label=html_escape(badge_label),
        right_html=right_html,
        body=body,
        grid_cols=grid_cols,
        tap_size=tap_size,
        name_size=name_size,
        sub_size=sub_size,
        abv_size=abv_size,
    ))


# ---------- Main ------------------------------------------------------------


# ---------- JSON output (for website / API consumers) ----------------------


def render_json(beers: Iterable["Beer"], out_path: Path, bar_name: str) -> None:
    """Write a clean JSON file with the current beer list.

    Designed for our Vercel-hosted brewery website to fetch and render.
    GitHub Pages serves with CORS allowed, so client-side fetch() works
    from any origin.

    Schema is stable — if you change it, also notify the website devs.
    """
    beers = list(beers)
    payload = {
        "location": bar_name,
        "updated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tap_count": len(beers),
        "beers": [
            {
                "tap":   b.tap,
                "name":  b.name,
                "style": b.style,
                "abv":   b.abv,
                "price": b.price,
                "note":  b.note,
                "empty": not b.name,
            }
            for b in beers
        ],
    }
    out_path.write_text(json.dumps(payload, indent=2))


# ---------- Embeddable lite HTML (for the brewery website) -----------------

EMBED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{bar_name} — On Tap</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<script>
  // Refresh every 60s with a cache-buster so the embed stays current
  setTimeout(function() {{
    var u = new URL(window.location.href);
    u.searchParams.set('t', Date.now());
    window.location.replace(u.toString());
  }}, 60000);
</script>
<style>
  {font_face_rules}
  :root {{
    --orange: {c_orange};
    --sage:   {c_sage};
    --text:   {c_text};
    --bg:     {c_bg};
    --lime:   {c_lime};
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: '{body_font_name}', 'Helvetica Neue', Helvetica, Arial, sans-serif;
  }}
  .embed-wrap {{
    padding: 1.5rem 1.25rem;
    max-width: 1200px;
    margin: 0 auto;
  }}
  .embed-title {{
    font-family: '{title_font_name}', Georgia, serif;
    color: var(--orange);
    font-size: 2.6rem;
    line-height: 1;
    text-align: center;
    margin: 0 0 1.5rem 0;
  }}
  ul.beers {{
    list-style: none; margin: 0; padding: 0;
    display: grid;
    grid-template-columns: 1fr;
    gap: 0;
  }}
  @media (min-width: 700px)  {{ ul.beers {{ grid-template-columns: 1fr 1fr; column-gap: 2.5rem; }} }}
  @media (min-width: 1100px) {{ ul.beers {{ grid-template-columns: 1fr 1fr 1fr; column-gap: 2.5rem; }} }}
  ul.beers li {{
    display: grid;
    grid-template-columns: 2.5rem minmax(0, 1fr) auto;
    column-gap: 0.75rem;
    align-items: center;
    padding: 0.55rem 0;
    border-bottom: 1px solid color-mix(in srgb, var(--lime) 45%, transparent);
    min-width: 0;
  }}
  .tap {{
    font-family: '{title_font_name}', Georgia, serif;
    font-size: 1.5rem; line-height: 1;
    color: var(--sage); font-weight: normal;
    text-align: left;
  }}
  .tap.empty {{ opacity: 0.45; }}
  .beer {{ min-width: 0; overflow: hidden; }}
  .beer .name {{
    color: var(--orange);
    font-weight: 800;
    font-size: 1.05rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .beer .name.empty {{ color: var(--orange); opacity: 0.55; font-weight: 600; text-transform: none; }}
  .beer .substyle {{
    color: var(--text);
    font-size: 0.85rem;
    margin-top: 0.15rem;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0.85;
  }}
  .abv {{
    color: var(--text);
    font-size: 0.95rem;
    font-weight: 600;
    text-align: right;
    white-space: nowrap;
  }}
  .updated {{
    font-size: 0.75rem; color: var(--text); opacity: 0.5;
    margin-top: 1.5rem; text-align: center; font-style: italic;
  }}
</style>
</head>
<body>
<div class="embed-wrap">
<h2 class="embed-title">{location_short}</h2>
{body}
<div class="updated">Pulled live from Toast · {timestamp}</div>
</div>
</body>
</html>
"""


def render_embed_html(beers: Iterable["Beer"], out_path: Path, bar_name: str,
                      brand: dict | None = None) -> None:
    """Write a brand-styled, iframe-friendly list of the current draft beers.

    No header bar, no badge — just a clean, responsive list designed to drop
    inside the brewery website page that shows pictures of each location.
    Single column on narrow viewports, 2-3 columns on wider ones.
    """
    beers = list(beers)
    if brand is None:
        brand = load_brand(Path(__file__).resolve().parent)
    here = Path(__file__).resolve().parent

    empty_slot_text = (brand["header"].get("empty_slot_text") or "").strip()
    rows = "".join(_render_beer_li(b, empty_slot_text) for b in beers)
    body = f'<ul class="beers">{rows}</ul>'

    # Show just the location-specific part in the embed title.
    # e.g. "Fifty West - Deerfield" → "Deerfield".
    location_short = bar_name
    for sep in (" - ", " – ", "- ", "–"):
        if sep in bar_name:
            location_short = bar_name.split(sep, 1)[-1].strip()
            break

    F = brand["fonts"]
    out_path.write_text(EMBED_TEMPLATE.format(
        font_face_rules=_build_font_face_rules(brand, here),
        bar_name=html_escape(bar_name),
        location_short=html_escape(location_short),
        c_orange=brand["colors"]["accent_orange"],
        c_sage=brand["colors"]["accent_sage"],
        c_text=brand["colors"]["text_dark"],
        c_bg=brand["colors"]["background"],
        c_lime=brand["colors"].get("accent_lime", brand["colors"]["accent_sage"]),
        title_font_name=F.get("title_font_name") or "Times-Bold",
        body_font_name=F.get("body_font_name") or "Helvetica",
        body=body,
        timestamp=datetime.datetime.now().strftime("%b %-d, %Y · %-I:%M %p"),
    ))


def main() -> int:
    here = Path(__file__).resolve().parent
    load_dotenv(here / ".env")

    p = argparse.ArgumentParser(description="Generate draft beer list from Toast POS.")
    p.add_argument("--sample", action="store_true", help="Use sample_menu.json instead of calling Toast.")
    p.add_argument("--output-dir", default=str(here), help="Where to write outputs.")
    p.add_argument("--pdf-only", action="store_true")
    p.add_argument("--html-only", action="store_true")
    p.add_argument("--group-name", default=None, help="Override DRAFT_GROUP_NAME for this run.")
    p.add_argument("--menu-name", default=None,
                   help="Override DRAFT_MENU_NAME — only pull from this parent menu.")
    p.add_argument("--list-groups", action="store_true",
                   help="Diagnostic: pull menus from Toast and print every menu group + item count, then exit.")
    p.add_argument("--save-payload", default=None,
                   help="Diagnostic: save the raw Toast menu JSON to this path for inspection.")
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.sample:
        payload = json.loads((here / "sample_menu.json").read_text())
        print(f"Using sample_menu.json ({len(payload.get('menus', []))} menu(s))")
    else:
        print("Fetching menu from Toast...")
        payload = fetch_menu_from_toast()

    if args.save_payload:
        Path(args.save_payload).write_text(json.dumps(payload, indent=2))
        print(f"  Saved raw payload to {args.save_payload}")

    if args.list_groups:
        print("\n=== Menu groups in this restaurant ===")
        any_groups = False
        for menu in payload.get("menus", []):
            menu_name = menu.get("name", "<unnamed>")
            for group in menu.get("menuGroups", []):
                any_groups = True
                gname = group.get("name", "<unnamed>")
                items = group.get("menuItems", []) or []
                print(f"  Menu '{menu_name}' / Group '{gname}'  →  {len(items)} item(s)")
                for it in items[:5]:
                    desc = (it.get("description") or "").strip()
                    desc_preview = (desc[:60] + "…") if len(desc) > 60 else desc
                    print(f"      - {it.get('name'):30s}  ${float(it.get('price') or 0):5.2f}   {desc_preview}")
                if len(items) > 5:
                    print(f"      ... and {len(items) - 5} more")
        if not any_groups:
            print("  (no menu groups found — check that the credential set has menus:read scope)")
        return 0

    group_name = args.group_name or cfg("DRAFT_GROUP_NAME")
    menu_name = args.menu_name or cfg("DRAFT_MENU_NAME") or None
    bar_name = cfg("BAR_NAME")
    beers = extract_drafts(payload, group_name, menu_name=menu_name)
    scope = f"'{group_name}' group"
    if menu_name:
        scope = f"'{menu_name}' menu / {scope}"
    print(f"Found {len(beers)} beer(s) in {scope}.")

    # If EXPECTED_TAP_COUNT is set, pad to that many slots so the grid is
    # always the configured size (e.g., 24 = 3 cols × 8 rows). Empty taps
    # render as muted tap numbers with no beer info.
    try:
        expected = int(cfg("EXPECTED_TAP_COUNT") or 0)
    except ValueError:
        expected = 0
    if expected > 0:
        beers = fill_taps(beers, expected)
        active = sum(1 for b in beers if b.name)
        if active < expected:
            print(f"  Padded to {expected} slots ({expected - active} empty tap(s)).")

    brand = load_brand(here)
    register_brand_fonts(brand, here)

    if not args.html_only:
        pdf_path = out_dir / "draft_list.pdf"
        render_pdf(beers, pdf_path, bar_name, brand=brand)
        print(f"  Wrote {pdf_path}")
    if not args.pdf_only:
        html_path = out_dir / "draft_list.html"
        render_html(beers, html_path, bar_name, brand=brand)
        print(f"  Wrote {html_path}")
        # Website-facing outputs: JSON for the Vercel devs to fetch+render,
        # and a brand-styled embeddable list they can iframe instead.
        embed_path = out_dir / "embed.html"
        render_embed_html(beers, embed_path, bar_name, brand=brand)
        print(f"  Wrote {embed_path}")
        json_path = out_dir / "beers.json"
        render_json(beers, json_path, bar_name)
        print(f"  Wrote {json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
