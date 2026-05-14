"""
scrape_mn_scores.py — Minnesota-Scores.net adapter

Scrapes section standings from https://www.minnesota-scores.net to supplement
MaxPreps data for MN boys basketball.

What this provides per team:
  • MSHSL section assignment  (section_id / section_name)
  • Section W/L record        (conf_wins / conf_losses, record_type="section")
  • Overall W/L record        (ovr_wins / ovr_losses)
  • QRF quality rating        (rating)
  • Logo URL when present     (logo_url)

Outer-merge notes
-----------------
Some teams appear on mn-scores but NOT MaxPreps (small / new programs).
Some MaxPreps teams are not on mn-scores (data gaps on either side).
This adapter returns only what mn-scores has; scrape_hs_leagues.py decides
how to handle unmatched teams.

TODO: section-as-conference reorganisation
  Once this data is used, a future pass should reorganise MN output so that
  MSHSL sections become the "conferences" in the data pack (sections determine
  playoff seeding, which maps better to Campus Hoops tournament structure).
  That requires restructuring conf_teams_map in scrape_state.
"""

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from supplements import TeamSupplement, norm_name, merge_supplement

BASE_URL   = "https://www.minnesota-scores.net"
SPORT_PATH = "boys-sports/basketball"
CACHE_DIR  = Path(".scrape_cache")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://www.minnesota-scores.net/",
}

# Classes in ascending order — important for substring-safe matching
_CLASSES = ["AAAA", "AAA", "AA", "A"]


# ── Session ────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


# ── Section ID helpers ─────────────────────────────────────────────────────────

def _section_key(cls: str, num: int, suffix: str = "") -> str:
    """Canonical conference-id string used in the data pack."""
    key = f"mn_section_{num}{cls.lower()}"
    return f"{key}_{suffix.lower()}" if suffix else key


def _section_display(cls: str, num: int, suffix: str = "") -> str:
    label = f"Section {num}{cls}"
    return f"{label} {suffix.capitalize()}" if suffix else label


# ── Page-ID discovery ──────────────────────────────────────────────────────────

def probe_class_page_ids(
    season: str,
    session: requests.Session | None = None,
) -> dict[str, int]:
    """
    Probe numeric page IDs to find one page per class (A / AA / AAA / AAAA)
    for the given season.  Results are cached so this only runs once per season.

    Returns {"A": 136, "AA": 137, "AAA": 138, "AAAA": 139} (example).
    """
    cache_file = CACHE_DIR / f"mn_scores_ids_{season.replace('-', '_')}.json"
    CACHE_DIR.mkdir(exist_ok=True)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    s = session or _session()
    result: dict[str, int] = {}

    # Known starting point (136 = Class A for 2025-2026); probe ±80 from there.
    for sid in list(range(136, 216)) + list(range(80, 136)):
        if len(result) == len(_CLASSES):
            break
        url = f"{BASE_URL}/{SPORT_PATH}/section-standings/{season}/{sid}"
        try:
            r = s.get(url, timeout=10)
        except requests.RequestException:
            time.sleep(0.1)
            continue
        if r.status_code != 200:
            time.sleep(0.05)
            continue
        text = r.text
        # Match longest class first to avoid "A" matching inside "AA"
        for cls in _CLASSES:
            if cls not in result and f"Minnesota {cls}" in text:
                result[cls] = sid
                print(f"    mn-scores page id {sid} → Class {cls}")
                break
        time.sleep(0.15)

    if result:
        cache_file.write_text(json.dumps(result))
    if len(result) < len(_CLASSES):
        missing = [c for c in _CLASSES if c not in result]
        print(f"  [warn] mn-scores: could not find page IDs for classes: {missing}")

    return result


# ── HTML parsing ───────────────────────────────────────────────────────────────

_WL_RE   = re.compile(r"^(\d+)-(\d+)$")
_SEC_RE  = re.compile(r"[Ss]ection\s+(\d+)(?:\s*[-–]\s*(\w+))?")
_FLOAT_RE = re.compile(r"^-?\d+\.?\d*$")


def _parse_wl(text: str) -> tuple[int, int] | None:
    m = _WL_RE.match(text.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _col_map(headers: list[str]) -> dict[str, int]:
    """
    Map logical column names to their index given a list of header strings.
    Handles both split-column (separate W / L) and combined "W-L" layouts.
    """
    col: dict[str, int] = {}
    for i, raw in enumerate(headers):
        h = raw.strip().lower()
        # Team name
        if "team" in h and "team" not in col:
            col["team"] = i
        # Section / conference record — split columns
        elif re.match(r"^(cw|sw|s\.?w\.?|section\s*w(ins?)?)$", h) and "conf_w" not in col:
            col["conf_w"] = i
        elif re.match(r"^(cl|sl|s\.?l\.?|section\s*l(oss(es)?)?)$", h) and "conf_l" not in col:
            col["conf_l"] = i
        # Section / conference record — combined "W-L" column
        elif re.match(r"^section$", h) and "conf_wl" not in col:
            col["conf_wl"] = i
        # NCW / NCL — non-conference, skip
        elif re.match(r"^nc[wl]$", h):
            pass
        # Overall record — split
        elif re.match(r"^(w|ow|wins?)$", h) and "ovr_w" not in col:
            col["ovr_w"] = i
        elif re.match(r"^(l|ol|loss(es)?)$", h) and "ovr_l" not in col:
            col["ovr_l"] = i
        # Overall record — combined
        elif re.match(r"^overall$", h) and "ovr_wl" not in col:
            col["ovr_wl"] = i
        # QRF value (skip "QRF Rank")
        elif "qrf" in h and "rank" not in h and "qrf" not in col:
            col["qrf"] = i

    return col


def _parse_class_page(html: str, cls: str) -> list[dict]:
    """
    Parse one class page (e.g., Class A — all 8 sections shown together).
    Returns a list of row dicts with keys:
        name, norm, section_id, section_name,
        conf_w, conf_l, ovr_w, ovr_l, qrf, logo_url
    """
    soup  = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []

    # Walk the DOM top-to-bottom, tracking the most recent section heading.
    current_num    = 0
    current_suffix = ""

    for el in soup.descendants:
        if not hasattr(el, "name") or not el.name:
            continue

        # ── Section heading detection ──────────────────────────────────────────
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6", "caption", "strong"):
            text = el.get_text(" ", strip=True)
            m = _SEC_RE.search(text)
            if m:
                current_num    = int(m.group(1))
                current_suffix = (m.group(2) or "").strip()
                continue

        # ── Table rows ────────────────────────────────────────────────────────
        if el.name != "table" or current_num == 0:
            continue

        cells_rows = el.find_all("tr")
        if not cells_rows:
            continue

        # Identify columns from the first row
        header_cells = cells_rows[0].find_all(["th", "td"])
        headers      = [c.get_text(" ", strip=True) for c in header_cells]
        col          = _col_map(headers)

        # If we can't locate a team column at all, try positional heuristic:
        # assume the second cell (index 1) is the team name.
        if "team" not in col and len(headers) >= 2:
            col["team"] = 1

        sid    = _section_key(cls, current_num, current_suffix)
        sname  = _section_display(cls, current_num, current_suffix)

        for row in cells_rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]

            # Skip sub-headers that got wrapped in <tr>
            joined = " ".join(texts[:4]).lower()
            if any(kw in joined for kw in ("team", "seed", "section", "overall", "qrf")):
                continue

            # ── Team name ──────────────────────────────────────────────────────
            name     = ""
            logo_url = ""
            tcell    = cells[col.get("team", 1)]
            a_tag    = tcell.find("a")
            name     = (a_tag or tcell).get_text(" ", strip=True)
            img      = tcell.find("img")
            if img and img.get("src", ""):
                src      = img["src"]
                logo_url = (BASE_URL + src) if src.startswith("/") else src

            name = name.strip()
            if not name or len(name) < 2 or name.isdigit():
                continue

            # ── Records ────────────────────────────────────────────────────────
            conf_w = conf_l = ovr_w = ovr_l = None

            if "conf_wl" in col:
                wl = _parse_wl(texts[col["conf_wl"]])
                if wl:
                    conf_w, conf_l = wl
            else:
                if "conf_w" in col:
                    try: conf_w = int(texts[col["conf_w"]])
                    except ValueError: pass
                if "conf_l" in col:
                    try: conf_l = int(texts[col["conf_l"]])
                    except ValueError: pass

            if "ovr_wl" in col:
                wl = _parse_wl(texts[col["ovr_wl"]])
                if wl:
                    ovr_w, ovr_l = wl
            else:
                if "ovr_w" in col:
                    try: ovr_w = int(texts[col["ovr_w"]])
                    except ValueError: pass
                if "ovr_l" in col:
                    try: ovr_l = int(texts[col["ovr_l"]])
                    except ValueError: pass

            # Fallback: scan for all W-L patterns in the row
            if ovr_w is None and ovr_l is None:
                wl_found = [_parse_wl(t) for t in texts if _parse_wl(t)]
                if len(wl_found) >= 2:
                    conf_w, conf_l = wl_found[0]
                    ovr_w,  ovr_l  = wl_found[-1]
                elif len(wl_found) == 1:
                    ovr_w, ovr_l = wl_found[0]

            # ── QRF ───────────────────────────────────────────────────────────
            qrf = None
            if "qrf" in col:
                try:
                    qrf = float(texts[col["qrf"]])
                except ValueError:
                    pass

            rows.append({
                "name":         name,
                "norm":         norm_name(name),
                "section_id":   sid,
                "section_name": sname,
                "conf_w":       conf_w,
                "conf_l":       conf_l,
                "ovr_w":        ovr_w,
                "ovr_l":        ovr_l,
                "qrf":          qrf,
                "logo_url":     logo_url or None,
            })

    return rows


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch(
    season:  str,
    session: requests.Session | None = None,
) -> dict[str, TeamSupplement]:
    """
    Main entry: return {normalised_name → TeamSupplement} for all MN teams
    found on minnesota-scores.net for the given season.

    Key: norm_name(team_name) from supplements.py — must be used consistently
    when looking up teams from scrape_hs_leagues.py.

    Outer-merge note: teams present here but absent from MaxPreps are NOT
    added to the data pack automatically.  The caller receives this dict and
    decides how to handle unmatched entries.
    """
    s        = session or _session()
    page_ids = probe_class_page_ids(season, s)
    result:  dict[str, TeamSupplement] = {}
    total    = 0

    for cls in ["A", "AA", "AAA", "AAAA"]:
        page_id = page_ids.get(cls)
        if page_id is None:
            continue
        url = f"{BASE_URL}/{SPORT_PATH}/section-standings/{season}/{page_id}"
        try:
            r = s.get(url, timeout=15)
        except requests.RequestException as exc:
            print(f"  [warn] mn-scores Class {cls}: request failed — {exc}")
            continue
        if r.status_code != 200:
            print(f"  [warn] mn-scores Class {cls}: HTTP {r.status_code}")
            continue

        entries = _parse_class_page(r.text, cls)
        print(f"    mn-scores Class {cls}: {len(entries)} teams parsed")

        for e in entries:
            norm = e["norm"]
            if not norm:
                continue
            supp = TeamSupplement(
                ovr_wins=e["ovr_w"],
                ovr_losses=e["ovr_l"],
                conf_wins=e["conf_w"],
                conf_losses=e["conf_l"],
                record_type="section",
                rating=e["qrf"],
                logo_url=e["logo_url"],
                section_id=e["section_id"],
                section_name=e["section_name"],
                source="mn_scores",
            )
            if norm in result:
                # Team appears in multiple sections — keep better record
                result[norm] = merge_supplement(result[norm], supp)
            else:
                result[norm] = supp
            total += 1

        time.sleep(0.2)

    print(f"  mn-scores: {len(result)} unique teams across all classes")
    return result
