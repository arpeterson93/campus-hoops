#!/usr/bin/env python3
"""
scrape_hs_leagues.py — Campus Hoops HS League Data Pack Generator

Scrapes MaxPreps.com for all 50 states' boys basketball programs and produces
one Campus Hoops data pack JSON per state, ready to load in the Data Pack editor.

Usage:
    python scrape_hs_leagues.py              # all 50 states
    python scrape_hs_leagues.py mn ca tx     # specific states only

Output:
    hs_packs/{state}.json

Cache:
    .scrape_cache/   — HTTP responses are cached here; delete to force re-fetch.

Extra dependencies (add to requirements.txt):
    pip install requests beautifulsoup4 Pillow colorthief
"""

import json
import re
import sys
import time
import random
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from colorthief import ColorThief
    from PIL import Image
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    print("Warning: colorthief/Pillow not installed — colors will be omitted.")
    print("  pip install Pillow colorthief\n")


# ── Config ─────────────────────────────────────────────────────────────────────

SEASON     = "25-26"
BASE_URL   = "https://www.maxpreps.com"
CACHE_DIR  = Path(".scrape_cache")
OUTPUT_DIR = Path("hs_packs")

STATES = [
    "al","ak","az","ar","ca","co","ct","de","fl","ga",
    "hi","id","il","in","ia","ks","ky","la","me","md",
    "ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc",
    "sd","tn","tx","ut","vt","va","wa","wv","wi","wy",
]

STATE_NAMES = {
    "al":"Alabama",    "ak":"Alaska",        "az":"Arizona",       "ar":"Arkansas",
    "ca":"California", "co":"Colorado",      "ct":"Connecticut",   "de":"Delaware",
    "fl":"Florida",    "ga":"Georgia",       "hi":"Hawaii",        "id":"Idaho",
    "il":"Illinois",   "in":"Indiana",       "ia":"Iowa",          "ks":"Kansas",
    "ky":"Kentucky",   "la":"Louisiana",     "me":"Maine",         "md":"Maryland",
    "ma":"Massachusetts","mi":"Michigan",    "mn":"Minnesota",     "ms":"Mississippi",
    "mo":"Missouri",   "mt":"Montana",       "ne":"Nebraska",      "nv":"Nevada",
    "nh":"New Hampshire","nj":"New Jersey",  "nm":"New Mexico",    "ny":"New York",
    "nc":"North Carolina","nd":"North Dakota","oh":"Ohio",         "ok":"Oklahoma",
    "or":"Oregon",     "pa":"Pennsylvania",  "ri":"Rhode Island",  "sc":"South Carolina",
    "sd":"South Dakota","tn":"Tennessee",    "tx":"Texas",         "ut":"Utah",
    "vt":"Vermont",    "va":"Virginia",      "wa":"Washington",    "wv":"West Virginia",
    "wi":"Wisconsin",  "wy":"Wyoming",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_session = requests.Session()
_session.headers.update(_HEADERS)


# ── HTTP / Caching ─────────────────────────────────────────────────────────────

def _cache_path(url: str, binary: bool = False) -> Path:
    safe = re.sub(r"[^\w-]", "_", url)[:220]
    return CACHE_DIR / (safe + (".bin" if binary else ".html"))


def fetch_html(url: str, delay: tuple = (1.0, 2.5), retries: int = 3) -> str | None:
    path = _cache_path(url)
    if path.exists():
        return path.read_text(encoding="utf-8")

    for attempt in range(retries):
        time.sleep(random.uniform(*delay))
        try:
            resp = _session.get(url, timeout=20)
            if resp.status_code == 200:
                path.write_text(resp.text, encoding="utf-8")
                return resp.text
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    Rate-limited — waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            print(f"    HTTP {resp.status_code}: {url}")
        except requests.RequestException as exc:
            print(f"    Request error ({attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                time.sleep(5)
    return None


def fetch_bytes(url: str) -> bytes | None:
    path = _cache_path(url, binary=True)
    if path.exists():
        return path.read_bytes()
    time.sleep(random.uniform(0.2, 0.6))
    try:
        resp = _session.get(url, timeout=10)
        if resp.status_code == 200:
            path.write_bytes(resp.content)
            return resp.content
    except Exception:
        pass
    return None


# ── Color Extraction ──────────────────────────────────────────────────────────

def _rgb_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _useful_color(r: int, g: int, b: int) -> bool:
    # Skip near-white and near-black; they dominate logos but aren't team colors
    brightness = (r + g + b) / 3
    return 25 < brightness < 230


def extract_colors(img_bytes: bytes) -> tuple[str, str]:
    if not HAS_COLOR:
        return "#888888", "#FFFFFF"
    try:
        img = Image.open(BytesIO(img_bytes))
        # Flatten transparency onto white background
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
        else:
            bg.paste(img.convert("RGB"))
        buf = BytesIO()
        bg.save(buf, format="PNG")
        buf.seek(0)

        ct = ColorThief(buf)
        palette = ct.get_palette(color_count=6, quality=1)
        useful = [c for c in palette if _useful_color(*c)]
        primary   = useful[0] if useful else palette[0]
        secondary = useful[1] if len(useful) > 1 else (255, 255, 255)
        return _rgb_hex(*primary), _rgb_hex(*secondary)
    except Exception:
        return "#888888", "#FFFFFF"


# ── Name / Slug Helpers ───────────────────────────────────────────────────────

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_mascot(team_slug: str, school_name: str) -> str:
    """
    /mn/farmington/farmington-tigers/basketball/
    team_slug='farmington-tigers', school_name='Farmington'
    → remove 'farmington-' prefix → 'tigers' → 'Tigers'
    """
    name_slug = slugify(school_name)
    if team_slug.startswith(name_slug + "-"):
        rest = team_slug[len(name_slug) + 1:]
        return rest.replace("-", " ").title()
    # Fallback: last word of slug
    return team_slug.split("-")[-1].title()


def make_abbreviation(name: str, max_len: int = 4) -> str:
    words = [w for w in name.upper().split() if w.isalpha()]
    if not words:
        return name[:max_len].upper()
    if len(words) == 1:
        return words[0][:max_len]
    initials = "".join(w[0] for w in words)
    return initials[:max_len] if len(initials) >= 2 else words[0][:max_len]


def clean_logo_url(raw: str) -> str:
    if not raw:
        return ""
    base = raw.split("?")[0]
    return f"{base}?width=200&height=200&auto=webp"


# ── Prestige & Conference Math ────────────────────────────────────────────────

def parse_record(s: str) -> tuple[int, int]:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def win_pct_prestige(wins: int, losses: int) -> int:
    total = wins + losses
    pct = wins / total if total > 0 else 0.40
    return max(35, min(90, round(35 + pct * 55)))


def conf_floor(min_pres: int) -> int:
    return round(max(15, 0.70 * min_pres) / 5) * 5


def conf_ceiling(max_pres: int) -> int:
    return round(min(95, 1.15 * max_pres) / 5) * 5


def conf_abbr(name: str) -> str:
    """
    'AAAA Section 1'       → '4A-S1'
    'Class 5A District 3'  → '5A-D3'
    'Region 2'             → 'R2'
    """
    up = name.upper()

    # Leading A-run: AAAA → 4A, AA → 2A, etc.
    a_run = re.match(r"^(A+)\s", up)
    num_a  = re.match(r"^(\d+)A\b", up)
    cls_match = re.search(r"\bclass\s+(\d+A|A+)\b", up)

    if a_run:
        cls = f"{len(a_run.group(1))}A"
    elif num_a:
        cls = f"{num_a.group(1)}A"
    elif cls_match:
        raw_cls = cls_match.group(1)
        cls = f"{len(raw_cls)}A" if raw_cls.isalpha() else raw_cls
    else:
        cls = ""

    # Section / District / Region number
    sec  = re.search(r"\bsection\s*(\d+)\b",  up)
    dist = re.search(r"\bdistrict\s*(\d+)\b", up)
    reg  = re.search(r"\bregion\s*(\d+)\b",   up)
    div  = re.search(r"\bdivision\s*(\d+)\b", up)
    num_part = ""
    if sec:
        num_part = f"S{sec.group(1)}"
    elif dist:
        num_part = f"D{dist.group(1)}"
    elif reg:
        num_part = f"R{reg.group(1)}"
    elif div:
        num_part = f"Div{div.group(1)}"

    if cls and num_part:
        return f"{cls}-{num_part}"
    if cls:
        return cls
    if num_part:
        return num_part
    # Last resort: first letters of each word, up to 5 chars
    return "".join(w[0] for w in name.split() if w)[:5].upper()


# ── Conference Discovery (state page) ─────────────────────────────────────────

def scrape_conferences(state: str) -> list[dict]:
    url  = f"{BASE_URL}/{state}/basketball/"
    html = fetch_html(url)
    if not html:
        print(f"  Could not fetch state page for {state.upper()}")
        return []

    soup    = BeautifulSoup(html, "html.parser")
    pattern = re.compile(
        rf"^/{re.escape(state)}/basketball/\d{{2}}-\d{{2}}/conference/([^/?#]+)",
        re.IGNORECASE,
    )
    seen:  set[str]   = set()
    confs: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0]
        m    = pattern.match(href)
        if not m:
            continue
        slug = m.group(1).rstrip("/")
        if slug in seen:
            continue
        seen.add(slug)

        full_url = (BASE_URL + href) if href.startswith("/") else href
        name     = a.get_text(separator=" ", strip=True) or slug.replace("-", " ").title()
        confs.append({"name": name, "id": slug, "url": full_url})

    return confs


# ── Team Discovery (conference page) ──────────────────────────────────────────

def _try_next_data(html: str, state: str) -> list[dict]:
    """Extract teams from Next.js __NEXT_DATA__ JSON blob if present."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html, re.DOTALL,
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    url_pat = re.compile(
        rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball", re.IGNORECASE
    )
    teams: list[dict] = []
    _walk(data, state, url_pat, teams, depth=0)
    return teams


def _walk(node, state: str, url_pat, out: list, depth: int):
    if depth > 14 or len(out) > 600:
        return
    if isinstance(node, dict):
        url  = node.get("url") or node.get("schoolUrl") or node.get("teamUrl") or ""
        name = (node.get("name") or node.get("schoolName") or node.get("teamName") or "").strip()
        logo = node.get("logoUrl") or node.get("logo") or node.get("mascotUrl") or ""
        rec  = str(node.get("overallRecord") or node.get("record") or "")
        m    = url_pat.search(url)
        if m and name and len(name) > 1:
            out.append({
                "name":       name,
                "city_slug":  m.group(1),
                "team_slug":  m.group(2),
                "record_str": rec,
                "logo_src":   str(logo),
            })
        for v in node.values():
            _walk(v, state, url_pat, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, state, url_pat, out, depth + 1)


def _try_html_parse(html: str, state: str) -> list[dict]:
    """Fallback: find team links directly in the HTML."""
    soup    = BeautifulSoup(html, "html.parser")
    pat     = re.compile(rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball/?", re.IGNORECASE)
    seen:   set[str]   = set()
    teams:  list[dict] = []

    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if not m:
            continue
        city_slug = m.group(1)
        team_slug = m.group(2)
        if team_slug in seen or team_slug == "basketball":
            continue
        seen.add(team_slug)

        name = a.get_text(separator=" ", strip=True)
        if not name or len(name) < 2:
            continue

        logo_src = ""
        for scope in (a, a.parent, getattr(a.parent, "parent", None)):
            if scope is None:
                continue
            img = scope.find("img")
            if img and img.get("src") and "maxpreps" in img.get("src", ""):
                logo_src = img["src"]
                break

        record_str = ""
        row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
        if row:
            for text in row.stripped_strings:
                if re.match(r"^\d+-\d+$", text):
                    record_str = text
                    break

        teams.append({
            "name":       name,
            "city_slug":  city_slug,
            "team_slug":  team_slug,
            "record_str": record_str,
            "logo_src":   logo_src,
        })

    return teams


def scrape_teams(conf: dict, state: str) -> list[dict]:
    html = fetch_html(conf["url"])
    if not html:
        return []

    teams = _try_next_data(html, state)
    if not teams:
        teams = _try_html_parse(html, state)

    # Deduplicate by team_slug
    seen:   set[str]   = set()
    unique: list[dict] = []
    for t in teams:
        slug = t.get("team_slug", "")
        if slug and slug not in seen:
            seen.add(slug)
            unique.append(t)
    return unique


# ── Pack Assembly ─────────────────────────────────────────────────────────────

def build_team(raw: dict, conf_id: str, state: str) -> dict:
    name      = raw["name"]
    team_slug = raw["team_slug"]
    wins, losses = parse_record(raw.get("record_str", ""))
    prestige  = win_pct_prestige(wins, losses)
    mascot    = extract_mascot(team_slug, name)
    abbr      = make_abbreviation(name)

    logo_src  = raw.get("logo_src", "")
    logo_url  = clean_logo_url(logo_src)
    primary, secondary = "#888888", "#FFFFFF"
    if logo_url:
        img_bytes = fetch_bytes(logo_url)
        if img_bytes:
            primary, secondary = extract_colors(img_bytes)

    return {
        "id":             team_slug,
        "name":           name,
        "mascot":         mascot,
        "abbreviation":   abbr,
        "conferenceId":   conf_id,
        "state":          state.upper(),
        "pipelineStates": [state.upper()],
        "offenseRating":  prestige,
        "defenseRating":  prestige,
        "prestige":       prestige,
        "primaryColor":   primary,
        "secondaryColor": secondary,
        "logoUrl":        logo_url or None,
    }


def build_conf(conf: dict, team_entries: list[dict]) -> dict:
    prestiges = [t["prestige"] for t in team_entries]
    floor   = conf_floor(min(prestiges))   if prestiges else 35
    ceiling = conf_ceiling(max(prestiges)) if prestiges else 85
    return {
        "id":              conf["id"],
        "name":            conf["name"],
        "abbreviation":    conf_abbr(conf["name"]),
        "isPower":         False,
        "hasTournament":   True,
        "conferenceGames": None,
        "prestigeFloor":   floor,
        "prestigeCeiling": ceiling,
        "logoUrl":         None,
    }


def scrape_state(state: str) -> dict:
    state_name = STATE_NAMES[state]
    print(f"\n{'=' * 60}")
    print(f"  {state.upper()} — {state_name}")
    print(f"{'=' * 60}")

    confs_raw = scrape_conferences(state)
    print(f"  {len(confs_raw)} conference(s) found")

    all_teams: list[dict] = []
    all_confs: list[dict] = []

    for conf in confs_raw:
        raw_teams    = scrape_teams(conf, state)
        team_entries = [build_team(t, conf["id"], state) for t in raw_teams]
        conf_entry   = build_conf(conf, team_entries)
        all_confs.append(conf_entry)
        all_teams.extend(team_entries)
        print(f"    [{conf['name']}]  {len(team_entries)} teams")

    # Sort conferences by name, teams by conference then name
    all_confs.sort(key=lambda c: c["name"].lower())
    name_map = {c["id"]: c["name"] for c in all_confs}
    all_teams.sort(key=lambda t: (
        name_map.get(t["conferenceId"], "").lower(),
        t["name"].lower(),
    ))

    return {
        "meta": {
            "name":        f"{state_name} HS Basketball",
            "version":     1,
            "author":      "Campus Hoops Scraper",
            "description": (
                f"Boys basketball — {state_name}. "
                f"Scraped from MaxPreps.com (season {SEASON}). "
                "Prestige derived from win percentage."
            ),
        },
        "conferences": all_confs,
        "teams":       all_teams,
    }


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    targets = [s.lower() for s in sys.argv[1:]] if len(sys.argv) > 1 else STATES
    bad     = [s for s in targets if s not in STATES]
    if bad:
        print(f"Unknown state(s): {', '.join(bad)}")
        print(f"Valid codes: {', '.join(STATES)}")
        sys.exit(1)

    print(f"Scraping {len(targets)} state(s): {', '.join(s.upper() for s in targets)}")
    print(f"Output  → {OUTPUT_DIR}/")
    print(f"Cache   → {CACHE_DIR}/  (delete folder to force re-fetch)\n")

    for state in targets:
        pack = scrape_state(state)
        out  = OUTPUT_DIR / f"{state}.json"
        out.write_text(json.dumps(pack, indent=2), encoding="utf-8")
        print(
            f"  Saved {out}  "
            f"({len(pack['teams'])} teams, {len(pack['conferences'])} confs)"
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
