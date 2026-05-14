#!/usr/bin/env python3
"""
scrape_hs_leagues.py — Campus Hoops HS League Data Pack Generator

Scrapes MaxPreps.com for all 50 states' boys basketball programs and produces
one Campus Hoops data pack JSON per state, ready to load in the Data Pack editor.

Ratings  (offenseRating / defenseRating): normalized from MaxPreps's current-season
           Rating metric, scaled to 50–99 within each state's ranked pool.

Prestige:  weighted average of the past 7 seasons' normalized ratings, scaled
           to 25–95.  More recent seasons carry more weight.

Unranked:  Teams with games played but below the rankings threshold are inferred
           from their position in the conference standings relative to ranked peers
           (linear interpolation / extrapolation).

Zero-game: Teams with 0 overall games are excluded (ghost conference registrations).

Usage:
    python scrape_hs_leagues.py              # all 50 states
    python scrape_hs_leagues.py mn ca tx     # specific states only

Output:    hs_packs/{state}.json
Cache:     .scrape_cache/  — delete to force re-fetch

Dependencies (beyond requirements.txt):
    pip install requests beautifulsoup4 Pillow colorthief
"""

import json
import re
import sys
import time
import random
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from supplements import TeamSupplement, merge_supplement, norm_name as _supp_norm

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

CURRENT_SEASON   = "25-26"
SEASONS_HISTORY  = ["25-26", "24-25", "23-24", "22-23", "21-22", "20-21", "19-20", "18-19", "17-18", "16-17"]
PRESTIGE_WEIGHTS = [0.26,    0.20,    0.15,    0.11,    0.08,    0.06,    0.05,    0.04,    0.03,    0.02]
# ^ sums to 1.0; most-recent year weighted most heavily

EXCLUDE_ZERO_RECORD = True   # drop teams with 0 overall games (conference ghosts)

# Secondary logo sources: state code → source identifier.
# When a team's MaxPreps logo is absent, the named source is tried as a fallback.
SECONDARY_LOGO_SOURCES: dict[str, str] = {
    "mn": "mshsl",
}

# Supplemental data sources per state.
# Each adapter returns {normalised_name → TeamSupplement} with better records,
# section assignments, QRF ratings, and logo URLs.
# Multiple sources per state are merged in order (later entries win on conflict).
SUPPLEMENTAL_SOURCES: dict[str, list[str]] = {
    "mn": ["mn_scores"],
}

# Conference-specific external standings (manual config).
# Maps MaxPreps leagueId → URL of a page that has a standings table.
# Used to supplement conference W/L records; record_type will be "conference".
CONF_EXTERNAL_STANDINGS: dict[str, str] = {
    "3848a71f-1457-4a4d-8a84-b7eb5197c5a8": "https://mcacathletics.org/boysbasketball/standings/",
}

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
    "al":"Alabama",      "ak":"Alaska",        "az":"Arizona",       "ar":"Arkansas",
    "ca":"California",   "co":"Colorado",      "ct":"Connecticut",   "de":"Delaware",
    "fl":"Florida",      "ga":"Georgia",       "hi":"Hawaii",        "id":"Idaho",
    "il":"Illinois",     "in":"Indiana",       "ia":"Iowa",          "ks":"Kansas",
    "ky":"Kentucky",     "la":"Louisiana",     "me":"Maine",         "md":"Maryland",
    "ma":"Massachusetts","mi":"Michigan",      "mn":"Minnesota",     "ms":"Mississippi",
    "mo":"Missouri",     "mt":"Montana",       "ne":"Nebraska",      "nv":"Nevada",
    "nh":"New Hampshire","nj":"New Jersey",    "nm":"New Mexico",    "ny":"New York",
    "nc":"North Carolina","nd":"North Dakota", "oh":"Ohio",          "ok":"Oklahoma",
    "or":"Oregon",       "pa":"Pennsylvania",  "ri":"Rhode Island",  "sc":"South Carolina",
    "sd":"South Dakota", "tn":"Tennessee",     "tx":"Texas",         "ut":"Utah",
    "vt":"Vermont",      "va":"Virginia",      "wa":"Washington",    "wv":"West Virginia",
    "wi":"Wisconsin",    "wy":"Wyoming",
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


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class TeamConf:
    """Data scraped from a conference standings page."""
    slug:        str
    name:        str
    city:        str
    city_slug:   str    # raw URL slug (e.g. "arden-hills"); used to build team page URLs
    conf_wins:   int
    conf_losses: int
    ovr_wins:    int
    ovr_losses:  int
    logo_src:    str
    page_order:  int    # 0-indexed position on page (preserves conference seeding)


@dataclass
class Ranking:
    """Data scraped from a state rankings page."""
    slug:     str
    name:     str
    rating:   float     # raw MaxPreps rating; can be negative
    strength: float
    wins:     int
    losses:   int
    city:     str = ""  # city slug from URL (e.g. "rochester")
    rank:     int = 0   # 1-based state rank position; 0 = unknown


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
    return 25 < (r + g + b) / 3 < 230


def extract_colors(img_bytes: bytes) -> tuple[str, str]:
    if not HAS_COLOR:
        return "#888888", "#FFFFFF"
    try:
        img = Image.open(BytesIO(img_bytes))
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
        else:
            bg.paste(img.convert("RGB"))
        buf = BytesIO()
        bg.save(buf, format="PNG")
        buf.seek(0)
        ct      = ColorThief(buf)
        palette = ct.get_palette(color_count=6, quality=1)
        useful  = [c for c in palette if _useful_color(*c)]
        primary   = useful[0] if useful else palette[0]
        secondary = useful[1] if len(useful) > 1 else (255, 255, 255)
        return _rgb_hex(*primary), _rgb_hex(*secondary)
    except Exception:
        return "#888888", "#FFFFFF"


# ── Name / Slug Helpers ───────────────────────────────────────────────────────

def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_mascot(team_slug: str, school_name: str) -> str:
    name_slug = slugify(school_name)
    if team_slug.startswith(name_slug + "-"):
        return team_slug[len(name_slug) + 1:].replace("-", " ").title()
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
    return raw.split("?")[0] + "?width=200&height=200&auto=webp"


def _parse_record(s: str) -> tuple[int, int]:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ── Fuzzy Name Matching ───────────────────────────────────────────────────────

# Words excluded from the word-overlap match — too generic to distinguish schools.
_MATCH_STOPWORDS = frozenset({
    "st", "saint", "and", "of", "the", "la",
    "christian", "lutheran", "methodist", "catholic", "baptist",
    "community", "central", "north", "south", "east", "west",
})


def _norm_name(name: str) -> str:
    """Lowercase, strip punctuation, remove generic school words."""
    n = name.lower()
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    for word in ("high school", "hs", "prep", "academy", "charter", "school"):
        n = re.sub(rf"\b{re.escape(word)}\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def match_ranking(
    conf_slug: str,
    conf_name: str,
    rankings: dict[str, "Ranking"],
) -> "Ranking | None":
    """
    Find a Ranking for a conference team.
    Priority: exact slug → exact name → slug containment (co-ops) → word-overlap name match.
    """
    if conf_slug in rankings:
        return rankings[conf_slug]

    # Exact normalized-name match — catches cases where the URL slug includes the
    # mascot on one page but not the other (e.g. "st-michael-albertville-knights"
    # vs "st-michael-albertville"), while both tables list the same school name.
    norm_conf = _norm_name(conf_name)
    if norm_conf:
        for r in rankings.values():
            if _norm_name(r.name) == norm_conf:
                return r

    # Slug containment — only when the shorter slug is long enough to be distinctive
    # (avoids matching on short generic fragments like "north" or "st-paul")
    for slug, r in rankings.items():
        shorter = slug if len(slug) <= len(conf_slug) else conf_slug
        if len(shorter) >= 15 and (conf_slug in slug or slug in conf_slug):
            return r

    # Word-overlap name match — strip stopwords, require ≥ 2 content-word hits
    conf_words = {w for w in norm_conf.split() if w not in _MATCH_STOPWORDS}
    if len(conf_words) < 2:
        return None     # too few distinctive words to match safely
    best: Ranking | None = None
    best_score = 0.0
    for r in rankings.values():
        # If the ranking entry has a clear city prefix and that city slug does not
        # appear anywhere in the conference team's slug, skip it — different school.
        if r.city and r.city not in conf_slug and not conf_slug.startswith(r.city[:4]):
            rank_words = {w for w in _norm_name(r.name).split() if w not in _MATCH_STOPWORDS}
            # Only proceed if the city word is also in the conf name (otherwise city mismatch)
            city_word = r.city.split("-")[0]   # first word of city slug (e.g. "rochester")
            if city_word and city_word not in conf_slug and len(city_word) > 3:
                continue
        rank_words = {w for w in _norm_name(r.name).split() if w not in _MATCH_STOPWORDS}
        overlap    = len(conf_words & rank_words)
        if overlap < 2:
            continue    # at least 2 shared content words required
        score = overlap / max(len(conf_words), len(rank_words))
        if score > best_score and score >= 0.60:
            best_score, best = score, r
    return best


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_state_rankings(rankings: dict[str, Ranking]) -> dict[str, float]:
    """Map raw MaxPreps Rating values to [0, 1] within the state pool.

    Using actual rating values (not rank position) means teams clustered at
    the same rating receive the same normalized score.
    """
    if not rankings:
        return {}
    vals = {s: r.rating for s, r in rankings.items()}
    mn, mx = min(vals.values()), max(vals.values())
    span = mx - mn
    if span == 0:
        return {s: 0.5 for s in vals}
    return {s: (v - mn) / span for s, v in vals.items()}


def to_offense_defense(norm: float) -> int:
    """[0, 1] → [50, 99]"""
    return max(50, min(99, round(50 + norm * 49)))


def to_prestige(norm: float) -> int:
    """[0, 1] → [25, 95]"""
    return max(25, min(95, round(25 + norm * 70)))


# ── Conference Standing Inference ─────────────────────────────────────────────

def _interpolate_positions(
    known:     list[tuple[int, float]],
    total:     int,
    win_rates: list[float] | None = None,
) -> list[float]:
    """
    Given [(position, score), ...] for state-ranked teams (position 0 = best),
    return a score for every position 0..total-1.  Scores are bounded to [0, 1].

    When win_rates is supplied (one entry per position, each in [0, 1]):
      • ≥2 known teams: fit a line through (win_rate, score) via least squares,
        then predict every position from its win rate.  This grounds unranked
        teams in their actual performance rather than assuming uniform quality
        gaps between standings positions.
      • 1 known team: anchor at that point with an empirical slope of 1.0.
      • Degenerate (all win rates identical): fall back to position interpolation.

    Without win_rates the legacy position-based interpolation/extrapolation is
    used (kept as fallback for states without supplemental record data).
    """
    if not known:
        if win_rates:
            # No ranked peers at all; estimate from win rate around the median
            return [max(0.0, min(1.0, 0.35 + (w - 0.5))) for w in win_rates]
        return [0.35] * total

    known = sorted(known)

    # ── Win-rate regression ────────────────────────────────────────────────────
    if win_rates and len(win_rates) == total:
        xs = [win_rates[pos] for pos, _ in known]
        ys = [score          for _, score in known]

        if len(known) >= 2:
            mx   = sum(xs) / len(xs)
            my   = sum(ys) / len(ys)
            var  = sum((x - mx) ** 2 for x in xs)
            if var > 1e-6:                         # non-degenerate
                a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
                b = my - a * mx
                result = [max(0.0, min(1.0, a * win_rates[i] + b)) for i in range(total)]
                for pos, score in known:           # pin exact values for ranked teams
                    if 0 <= pos < total:
                        result[pos] = score
                return result
            # All ranked teams have same win rate — fall through to position method

        elif len(known) == 1:
            anchor_pos, anchor_score = known[0]
            aw = win_rates[anchor_pos]
            result = [max(0.0, min(1.0, anchor_score + (win_rates[i] - aw)))
                      for i in range(total)]
            result[anchor_pos] = anchor_score
            return result

    # ── Legacy position-based interpolation / extrapolation ───────────────────
    result_opt: list[float | None] = [None] * total
    for pos, score in known:
        if 0 <= pos < total:
            result_opt[pos] = score

    for i in range(len(known) - 1):
        p1, s1 = known[i]
        p2, s2 = known[i + 1]
        for j in range(p1 + 1, p2):
            t = (j - p1) / (p2 - p1)
            result_opt[j] = s1 + t * (s2 - s1)

    first_p, first_s = known[0]
    if first_p > 0:
        step = ((first_s - known[1][1]) / (known[1][0] - first_p)
                if len(known) > 1 else 0.04)
        step = max(step, 0.01)
        for j in range(first_p - 1, -1, -1):
            result_opt[j] = min(1.0, first_s + step * (first_p - j))

    last_p, last_s = known[-1]
    if last_p < total - 1:
        step = ((known[-2][1] - last_s) / (last_p - known[-2][0])
                if len(known) > 1 else 0.04)
        step = max(step, 0.01)
        for j in range(last_p + 1, total):
            result_opt[j] = max(0.0, last_s - step * (j - last_p))

    return [float(r) if r is not None else 0.35 for r in result_opt]


def _best_conf_record(
    team: "TeamConf",
    supp: TeamSupplement | None,
) -> tuple[int, int]:
    """
    Return the conference/section win-loss record with the most games.
    Uses the supplement's conf record when it has more games than MaxPreps
    AND shares the same record_type as the MaxPreps conference (i.e. both
    are "conference" records, not section records).
    For section-type supplements the section record is intentionally ignored
    here — it covers a different set of opponents from the MaxPreps conference.
    """
    mp_w, mp_l = team.conf_wins, team.conf_losses
    if (supp is not None
            and supp.record_type == "conference"
            and supp.conf_wins  is not None
            and supp.conf_losses is not None):
        supp_total = supp.conf_wins + supp.conf_losses
        if supp_total > mp_w + mp_l:
            return supp.conf_wins, supp.conf_losses
    return mp_w, mp_l


def _lookup_supp(
    team: "TeamConf",
    supplements: dict[str, TeamSupplement],
) -> TeamSupplement | None:
    """Look up a team's supplement by normalised name; try token-overlap fallback."""
    norm = _supp_norm(team.name)
    if norm in supplements:
        return supplements[norm]
    # Partial-match fallback (handles short abbreviations / extra words)
    tokens = set(norm.split())
    for key, supp in supplements.items():
        key_tokens = set(key.split())
        shared = tokens & key_tokens
        shorter = min(len(tokens), len(key_tokens))
        if shorter >= 2 and len(shared) >= shorter:
            return supp
    return None


def infer_conference_scores(
    conf_teams:  list["TeamConf"],
    state_norm:  dict[str, float],
    rankings:    dict[str, "Ranking"],
    supplements: dict[str, TeamSupplement] | None = None,
) -> dict[str, float | None]:
    """
    Returns {team_slug: normalized_score [0,1]} for every team in the conference.
    slug → None means the team should be excluded (0 games played).

    When supplements are provided, conference win rates are computed from the
    better of the MaxPreps record and the supplement's same-conference record,
    then passed to _interpolate_positions for win-rate-based regression instead
    of position-based extrapolation.
    """
    supplements = supplements or {}
    result: dict[str, float | None] = {}

    # Partition into active (have played at least 1 game) and ghost (0-0)
    active: list["TeamConf"] = []
    for t in conf_teams:
        if EXCLUDE_ZERO_RECORD and (t.ovr_wins + t.ovr_losses) == 0:
            result[t.slug] = None
        else:
            active.append(t)

    if not active:
        return result

    active.sort(key=lambda t: t.page_order)
    n = len(active)

    # Build win-rate list for all active teams (used by regression interpolation)
    win_rates: list[float] = []
    for t in active:
        supp   = _lookup_supp(t, supplements)
        cw, cl = _best_conf_record(t, supp)
        total  = cw + cl
        win_rates.append(cw / total if total > 0 else 0.5)

    # Identify ranked teams
    known: list[tuple[int, float]] = []
    for i, t in enumerate(active):
        r = match_ranking(t.slug, t.name, rankings)
        if r is not None and r.slug in state_norm:
            if r.slug != t.slug:   # fuzzy match — validate with win-rate guard
                conf_total = t.ovr_wins + t.ovr_losses
                rank_total = r.wins + r.losses
                if conf_total >= 5 and rank_total >= 5:
                    conf_rate  = t.ovr_wins / conf_total
                    rank_rate  = r.wins / rank_total
                    if abs(conf_rate - rank_rate) > 0.50:
                        r = None
        if r is not None and r.slug in state_norm:
            known.append((i, state_norm[r.slug]))
        else:
            print(f"      [no ranking match] {t.name}  slug={t.slug}  ({t.ovr_wins}-{t.ovr_losses})")

    scores = _interpolate_positions(known, n, win_rates=win_rates)

    for i, t in enumerate(active):
        result[t.slug] = scores[i]

    return result


# ── Multi-Year Prestige ────────────────────────────────────────────────────────

def compute_prestige_norm(
    slug:          str,
    name:          str,
    season_norms:  dict[str, dict[str, float]],
    all_rankings:  dict[str, dict[str, Ranking]],
) -> float | None:
    """
    Weighted average of normalized scores across SEASONS_HISTORY.
    Returns None if the team has no data in any season.
    """
    total_w = 0.0
    total_s = 0.0

    for season, weight in zip(SEASONS_HISTORY, PRESTIGE_WEIGHTS):
        norm_map    = season_norms.get(season, {})
        ranking_map = all_rankings.get(season, {})

        # Direct slug lookup first
        score = norm_map.get(slug)
        if score is None:
            r = match_ranking(slug, name, ranking_map)
            if r is not None:
                score = norm_map.get(r.slug)

        if score is not None:
            total_s += score * weight
            total_w += weight

    return (total_s / total_w) if total_w > 1e-6 else None


# ── Conference Abbreviation / Floor / Ceiling ─────────────────────────────────

def conf_abbr(name: str) -> str:
    up = name.upper()
    # Leading A-run: AAAA → 4A, AA → 2A, etc.
    a_run   = re.match(r"^(A+)\s", up)
    num_a   = re.match(r"^(\d+)A\b", up)
    cls_any = re.search(r"\bclass\s+(\d+A|A+)\b", up, re.IGNORECASE)
    if a_run:
        cls = f"{len(a_run.group(1))}A"
    elif num_a:
        cls = f"{num_a.group(1)}A"
    elif cls_any:
        raw = cls_any.group(1)
        cls = f"{len(raw)}A" if raw.isalpha() else raw
    else:
        cls = ""

    sec  = re.search(r"\bsection\s*(\d+)\b",  up, re.IGNORECASE)
    dist = re.search(r"\bdistrict\s*(\d+)\b", up, re.IGNORECASE)
    reg  = re.search(r"\bregion\s*(\d+)\b",   up, re.IGNORECASE)
    div  = re.search(r"\bdivision\s*(\d+)\b", up, re.IGNORECASE)
    if sec:
        num_part = f"S{sec.group(1)}"
    elif dist:
        num_part = f"D{dist.group(1)}"
    elif reg:
        num_part = f"R{reg.group(1)}"
    elif div:
        num_part = f"Div{div.group(1)}"
    else:
        num_part = ""

    if cls and num_part:
        return f"{cls}-{num_part}"
    if cls:
        return cls
    if num_part:
        return num_part
    return "".join(w[0] for w in name.split() if w)[:5].upper()


def conf_floor(min_pres: int) -> int:
    return round(max(15, 0.70 * min_pres) / 5) * 5


def conf_ceiling(max_pres: int) -> int:
    return round(min(95, 1.15 * max_pres) / 5) * 5


# ── Rankings Scraping ─────────────────────────────────────────────────────────

def _rankings_url(state: str, season: str, page: int) -> str:
    return f"{BASE_URL}/{state}/basketball/{season}/rankings/{page}/"


def _next_data(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _walk_rankings(node, url_pat: re.Pattern, out: list[Ranking], depth: int):
    if depth > 14 or len(out) > 1000:
        return
    if isinstance(node, dict):
        url    = node.get("url") or node.get("schoolUrl") or node.get("teamUrl") or ""
        name   = (node.get("name") or node.get("schoolName") or node.get("teamName") or "").strip()
        rating = node.get("overallRating") or node.get("rating")
        m = url_pat.search(url)
        if m and name and len(name) > 1 and rating is not None:
            try:
                strength = float(
                    node.get("strengthOfSchedule") or node.get("strength") or 0
                )
                rec = str(node.get("overallRecord") or node.get("record") or "0-0")
                w, l = _parse_record(rec)
                rank_num = int(node.get("rank") or node.get("ranking") or node.get("teamRank") or 0)
                out.append(Ranking(
                    slug=m.group(2), name=name,
                    rating=float(rating), strength=strength,
                    wins=w, losses=l,
                    city=m.group(1),
                    rank=rank_num,
                ))
            except (ValueError, TypeError):
                pass
        for v in node.values():
            _walk_rankings(v, url_pat, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_rankings(item, url_pat, out, depth + 1)


def _rankings_from_next_data(html: str, state: str) -> list[Ranking]:
    data = _next_data(html)
    if not data:
        return []
    url_pat = re.compile(
        rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball", re.IGNORECASE
    )
    out: list[Ranking] = []
    _walk_rankings(data, url_pat, out, 0)
    return out


def _rankings_from_html(html: str, state: str) -> list[Ranking]:
    soup    = BeautifulSoup(html, "html.parser")
    url_pat = re.compile(
        rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball/?", re.IGNORECASE
    )
    seen: set[str] = set()
    out:  list[Ranking] = []

    for row in soup.find_all("tr"):
        link = row.find("a", href=url_pat)
        if not link:
            continue
        m = url_pat.search(link["href"])
        if not m:
            continue
        slug = m.group(2)
        if slug in seen:
            continue
        seen.add(slug)

        name  = link.get_text(separator=" ", strip=True)
        cells = row.find_all("td")

        # Column order: # | Team | Ovr. | Rating | Str. | +/-
        # Use cell positions directly — avoids float-scanning ambiguity from the +/- column.
        if len(cells) < 5:
            continue
        try:
            rating   = float(cells[3].get_text(strip=True))
            strength = float(cells[4].get_text(strip=True))
        except ValueError:
            continue

        row_text = row.get_text(" ")
        rec_m = re.search(r"(\d+)\s*[-–]\s*(\d+)", row_text)
        w, l  = (int(rec_m.group(1)), int(rec_m.group(2))) if rec_m else (0, 0)
        try:
            rank_num = int(cells[0].get_text(strip=True))
        except ValueError:
            rank_num = 0

        out.append(Ranking(slug=slug, name=name, rating=rating, strength=strength,
                           wins=w, losses=l, city=m.group(1), rank=rank_num))
    return out


def scrape_rankings_page(state: str, season: str, page: int) -> list[Ranking]:
    html = fetch_html(_rankings_url(state, season, page))
    if not html:
        return []
    entries = _rankings_from_next_data(html, state) or _rankings_from_html(html, state)
    return entries


def scrape_all_rankings(state: str, season: str) -> dict[str, Ranking]:
    """Fetch all pages of rankings for one state+season. Returns {slug: Ranking}."""
    result: dict[str, Ranking] = {}
    for page in range(1, 30):           # safety cap: 30 pages × 25 = 750 teams
        entries = scrape_rankings_page(state, season, page)
        if not entries:
            break
        for e in entries:
            if e.slug not in result:    # first occurrence preserves page order = rank order
                if e.rank == 0:
                    e.rank = len(result) + 1   # sequential fallback if JSON had no rank field
                result[e.slug] = e
        if len(entries) < 25:           # last page is always shorter
            break
    return result


# ── Conference Page Scraping ──────────────────────────────────────────────────

def _walk_conf(node, url_pat: re.Pattern, out: list[TeamConf], depth: int):
    if depth > 14 or len(out) > 500:
        return
    if isinstance(node, dict):
        url  = node.get("url") or node.get("schoolUrl") or node.get("teamUrl") or ""
        name = (node.get("name") or node.get("schoolName") or node.get("teamName") or "").strip()
        logo = node.get("logoUrl") or node.get("logo") or node.get("mascotUrl") or ""
        m    = url_pat.search(url)
        if m and name and len(name) > 1:
            city_slug = m.group(1)
            team_slug = m.group(2)
            ovr_rec   = str(node.get("overallRecord") or node.get("record") or "")
            conf_rec  = str(node.get("conferenceRecord") or node.get("leagueRecord") or "")
            ow, ol    = _parse_record(ovr_rec)
            cw, cl    = _parse_record(conf_rec)
            out.append(TeamConf(
                slug=team_slug, name=name,
                city=city_slug.replace("-", " ").title(),
                city_slug=city_slug,
                conf_wins=cw, conf_losses=cl,
                ovr_wins=ow, ovr_losses=ol,
                logo_src=str(logo),
                page_order=len(out),
            ))
        for v in node.values():
            _walk_conf(v, url_pat, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_conf(item, url_pat, out, depth + 1)


def _conf_from_next_data(html: str, state: str) -> list[TeamConf]:
    data = _next_data(html)
    if not data:
        return []
    url_pat = re.compile(
        rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball", re.IGNORECASE
    )
    out: list[TeamConf] = []
    _walk_conf(data, url_pat, out, 0)
    return out


def _conf_from_html(html: str, state: str) -> list[TeamConf]:
    soup    = BeautifulSoup(html, "html.parser")
    url_pat = re.compile(
        rf"/{re.escape(state)}/([^/]+)/([^/]+)/basketball/?", re.IGNORECASE
    )
    seen: set[str] = set()
    out:  list[TeamConf] = []

    for a in soup.find_all("a", href=url_pat):
        m = url_pat.search(a["href"])
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
            if img and "maxpreps" in img.get("src", ""):
                logo_src = img["src"]
                break

        row  = a.find_parent("tr") or a.find_parent("li")
        recs = re.findall(r"\b(\d+)-(\d+)\b", row.get_text() if row else "")
        # MaxPreps typically shows conference record then overall record in the row
        cw, cl = (int(recs[0][0]), int(recs[0][1])) if len(recs) > 0 else (0, 0)
        ow, ol = (int(recs[1][0]), int(recs[1][1])) if len(recs) > 1 else (cw, cl)

        out.append(TeamConf(
            slug=team_slug, name=name,
            city=city_slug.replace("-", " ").title(),
            city_slug=city_slug,
            conf_wins=cw, conf_losses=cl,
            ovr_wins=ow, ovr_losses=ol,
            logo_src=logo_src,
            page_order=len(out),
        ))
    return out


def scrape_conf_teams(conf: dict, state: str) -> list[TeamConf]:
    html = fetch_html(conf["url"])
    if not html:
        return []
    teams = _conf_from_next_data(html, state) or _conf_from_html(html, state)
    seen:   set[str] = set()
    unique: list[TeamConf] = []
    for t in teams:
        if t.slug not in seen:
            seen.add(t.slug)
            unique.append(t)
    return unique


# ── State Conference Discovery ────────────────────────────────────────────────

def scrape_conferences(state: str) -> list[dict]:
    url  = f"{BASE_URL}/{state}/basketball/"
    html = fetch_html(url)
    if not html:
        print(f"  Could not fetch state page for {state.upper()}")
        return []

    soup    = BeautifulSoup(html, "html.parser")
    pattern = re.compile(
        rf"^/{re.escape(state)}/basketball/\d{{2}}-\d{{2}}"
        r"/(?:conference|region|district|section|division)/([^/?#]+)",
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


# ── Pack Assembly ─────────────────────────────────────────────────────────────

def scrape_team_colors(team: TeamConf, state: str) -> tuple[str, str] | None:
    """
    Fetch the school's MaxPreps profile page and return (primary, secondary) hex
    colors as entered by MaxPreps staff (e.g. '#00824B', '#FFFFFF').
    Returns None if the page is unreachable or no colors are stored.
    Falls back through color1→color2→color3→color4, skipping blanks.
    """
    url  = f"{BASE_URL}/{state}/{team.city_slug}/{team.slug}/"
    html = fetch_html(url, delay=(0.5, 1.5))
    if not html:
        return None
    data = _next_data(html)
    if not data:
        return None
    try:
        info = data["props"]["pageProps"]["schoolContext"]["schoolInfo"]
    except (KeyError, TypeError):
        return None

    def _pick(key: str) -> str | None:
        val = str(info.get(key) or "").strip()
        return f"#{val.upper()}" if len(val) == 6 else None

    colors = [c for c in (_pick(f"color{i}") for i in range(1, 5)) if c]
    if not colors:
        return None
    primary   = colors[0]
    secondary = colors[1] if len(colors) > 1 else "#FFFFFF"
    return primary, secondary


def build_team(
    team:          TeamConf,
    conf_id:       str,
    state:         str,
    od_norm:       float,       # current-season [0,1] → offense/defense
    prestige_norm: float,       # multi-year [0,1] → prestige
) -> dict:
    od       = to_offense_defense(od_norm)
    prestige = to_prestige(prestige_norm)
    mascot   = extract_mascot(team.slug, team.name)
    abbr     = make_abbreviation(team.name)
    logo_url = clean_logo_url(team.logo_src)

    # Prefer MaxPreps-entered school colors; fall back to logo extraction
    page_colors = scrape_team_colors(team, state)
    if page_colors:
        primary, secondary = page_colors
    else:
        primary, secondary = "#888888", "#FFFFFF"
        if logo_url:
            img_bytes = fetch_bytes(logo_url)
            if img_bytes:
                primary, secondary = extract_colors(img_bytes)

    return {
        "id":             team.slug,
        "name":           team.name,
        "mascot":         mascot,
        "abbreviation":   abbr,
        "conferenceId":   conf_id,
        "state":          state.upper(),
        "pipelineStates": [state.upper()],
        "offenseRating":  od,
        "defenseRating":  od,
        "prestige":       prestige,
        "primaryColor":   primary,
        "secondaryColor": secondary,
        "logoUrl":        logo_url or None,
    }


def build_conf(conf: dict, team_entries: list[dict]) -> dict:
    prestiges = [t["prestige"] for t in team_entries]
    return {
        "id":              conf["id"],
        "name":            conf["name"],
        "abbreviation":    conf_abbr(conf["name"]),
        "isPower":         False,
        "hasTournament":   True,
        "conferenceGames": None,
        "prestigeFloor":   conf_floor(min(prestiges)) if prestiges else 25,
        "prestigeCeiling": conf_ceiling(max(prestiges)) if prestiges else 95,
        "logoUrl":         None,
    }


# ── Secondary logo sources ────────────────────────────────────────────────────

def fill_secondary_logos(state: str, team_entries: list[dict]) -> int:
    """Fill missing logoUrls from a state-specific secondary source.
    Mutates team_entries in place. Returns the number of entries filled."""
    source = SECONDARY_LOGO_SOURCES.get(state.lower())
    if not source:
        return 0
    missing = [e for e in team_entries if not e.get("logoUrl")]
    if not missing:
        return 0

    if source == "mshsl":
        try:
            import scrape_mshsl as _mshsl
        except ImportError:
            print("  [warn] scrape_mshsl.py not found — skipping MSHSL logo fallback")
            return 0

        session = _mshsl._session()
        print(f"  MSHSL fallback: fetching school list for {len(missing)} teams…")
        school_list = _mshsl.fetch_school_list(session)
        print(f"    {len(school_list)} schools indexed")

        filled = 0
        for entry in missing:
            slug = _mshsl.find_match(entry["name"], school_list)
            if not slug:
                continue
            url = _mshsl.fetch_logo_url(slug, session)
            if url:
                entry["logoUrl"] = url
                filled += 1
                print(f"    {entry['name']} → {url}")
        print(f"  MSHSL: filled {filled}/{len(missing)} missing logos")
        return filled

    return 0


# ── Main Orchestration ────────────────────────────────────────────────────────

def scrape_state(state: str) -> dict:
    state_name = STATE_NAMES[state]
    print(f"\n{'=' * 60}")
    print(f"  {state.upper()} — {state_name}")
    print(f"{'=' * 60}")

    # 1. Discover all conferences / sections for this state
    confs_raw = scrape_conferences(state)
    print(f"  {len(confs_raw)} conference(s) found")

    # 2. Scrape teams per conference (preserves page ordering = conference seeding)
    conf_teams_map: dict[str, list[TeamConf]] = {}
    for conf in confs_raw:
        teams = scrape_conf_teams(conf, state)
        conf_teams_map[conf["id"]] = teams
        print(f"    [{conf['name']}]  {len(teams)} teams")

    # 3. Scrape rankings for all seasons (cached after first run)
    print(f"  Scraping rankings — {len(SEASONS_HISTORY)} seasons…")
    all_rankings: dict[str, dict[str, Ranking]] = {}
    for season in SEASONS_HISTORY:
        r = scrape_all_rankings(state, season)
        all_rankings[season] = r
        print(f"    {season}: {len(r)} ranked teams")

    # 4. Normalize raw ratings to [0, 1] within each season's state pool
    season_norms: dict[str, dict[str, float]] = {
        s: normalize_state_rankings(r) for s, r in all_rankings.items()
    }
    current_rankings = all_rankings[CURRENT_SEASON]
    current_norm     = season_norms[CURRENT_SEASON]

    # 5. Fetch supplemental data (state-specific secondary sources)
    supplements: dict[str, TeamSupplement] = {}
    for src_name in SUPPLEMENTAL_SOURCES.get(state.lower(), []):
        if src_name == "mn_scores":
            try:
                import scrape_mn_scores as _mn_scores
                print(f"  Fetching mn-scores supplements…")
                mn_supp = _mn_scores.fetch(CURRENT_SEASON)
                for norm, s in mn_supp.items():
                    supplements[norm] = (
                        merge_supplement(supplements[norm], s)
                        if norm in supplements else s
                    )
            except ImportError:
                print("  [warn] scrape_mn_scores.py not found — skipping")
        # Future sources: elif src_name == "other_source": ...

    # 6. Build team and conference entries
    all_team_entries: list[dict] = []
    all_conf_entries: list[dict] = []

    for conf in confs_raw:
        conf_teams = conf_teams_map.get(conf["id"], [])

        # Infer a current-season [0,1] score for every active team in the conference
        conf_scores = infer_conference_scores(
            conf_teams, current_norm, current_rankings, supplements=supplements
        )

        team_entries: list[dict] = []
        for t in conf_teams:
            od_norm = conf_scores.get(t.slug)
            if od_norm is None:
                continue    # excluded (0-0 record)

            # Multi-year prestige; fall back to current-season score if no history
            p_norm = compute_prestige_norm(t.slug, t.name, season_norms, all_rankings)
            if p_norm is None:
                p_norm = od_norm

            entry = build_team(t, conf["id"], state, od_norm, p_norm)
            team_entries.append(entry)

        if team_entries:
            all_conf_entries.append(build_conf(conf, team_entries))
            all_team_entries.extend(team_entries)

    # 7. Sort: conferences by name, teams by conference name then team name
    all_conf_entries.sort(key=lambda c: c["name"].lower())
    name_map = {c["id"]: c["name"] for c in all_conf_entries}
    all_team_entries.sort(key=lambda t: (
        name_map.get(t["conferenceId"], "").lower(),
        t["name"].lower(),
    ))

    excluded = sum(
        1 for t in [t for teams in conf_teams_map.values() for t in teams]
        if (t.ovr_wins + t.ovr_losses) == 0
    )
    print(f"  -> {len(all_team_entries)} teams included, {excluded} zero-game entries excluded")

    # 8. Fill missing logo URLs from secondary sources (e.g. MSHSL for MN)
    fill_secondary_logos(state, all_team_entries)

    return {
        "meta": {
            "name":        f"{state_name} HS Basketball",
            "version":     1,
            "author":      "Campus Hoops Scraper",
            "description": (
                f"Boys basketball — {state_name}. "
                f"Scraped from MaxPreps.com ({CURRENT_SEASON}). "
                f"Ratings: MaxPreps ranking score normalized 50–99. "
                f"Prestige: {len(SEASONS_HISTORY)}-season weighted average normalized 25–95. "
                f"Unranked teams (below games threshold) inferred from conference standing."
            ),
        },
        "conferences": all_conf_entries,
        "teams":       all_team_entries,
    }


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    CACHE_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    targets = [s.lower() for s in sys.argv[1:]] if len(sys.argv) > 1 else STATES
    bad     = [s for s in targets if s not in STATES]
    if bad:
        print(f"Unknown state code(s): {', '.join(bad)}")
        print(f"Valid: {', '.join(STATES)}")
        sys.exit(1)

    print(f"Scraping {len(targets)} state(s): {', '.join(s.upper() for s in targets)}")
    print(f"Output  -> {OUTPUT_DIR}/")
    print(f"Cache   -> {CACHE_DIR}/  (delete to force re-fetch)\n")

    for state in targets:
        try:
            pack = scrape_state(state)
            out  = OUTPUT_DIR / f"{state}.json"
            out.write_text(json.dumps(pack, indent=2), encoding="utf-8")
            print(
                f"  Saved {out}  "
                f"({len(pack['teams'])} teams, {len(pack['conferences'])} confs)"
            )
        except Exception as exc:
            print(f"  ERROR on {state.upper()}: {exc}")

    print("\nDone.")


if __name__ == "__main__":
    main()
