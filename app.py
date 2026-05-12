"""
Campus Hoops Utility — Streamlit UI

Run with:
    streamlit run app.py
"""

import io
import math
import os
import shutil
import tempfile
import zipfile

import pandas as pd
import streamlit as st
from better_profanity import profanity as _profanity

import database as db
import verifier as vf
from recruiting import POSITION_ABBR, RecruitingPool, fmt_height
from save_loader import SaveFile

_profanity.load_censor_words()

st.set_page_config(page_title="Campus Hoops Utility", layout="wide")
st.title("Campus Hoops Utility")


# ================================================================== helpers

def _is_clean(text: str) -> bool:
    return not _profanity.contains_profanity(text)


def _find_save_root(extracted_dir: str) -> str:
    contents = os.listdir(extracted_dir)
    if len(contents) == 1:
        candidate = os.path.join(extracted_dir, contents[0])
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "meta.json")):
            return candidate
    return extracted_dir


def _mark_dirty(page_key: str):
    st.session_state.setdefault("dirty_pages", set()).add(page_key)
    # Invalidate cached download bytes whenever a new edit is made
    st.session_state.pop("download_bytes", None)


def _coerce_val(new_val, original):
    """Coerce a value from a DataFrame cell to match the type of the original JSON value."""
    if original is None:
        return None if (isinstance(new_val, float) and math.isnan(new_val)) else new_val
    if isinstance(original, bool):
        if isinstance(new_val, float) and math.isnan(new_val):
            return original
        return bool(new_val)
    if isinstance(original, int):
        if isinstance(new_val, float) and math.isnan(new_val):
            return original
        try:
            return int(new_val)
        except (ValueError, TypeError):
            return original
    if isinstance(new_val, float) and math.isnan(new_val):
        return None
    return new_val


# ================================================================== coaches

_COACHES_DISPLAY_COLS = [
    "id", "firstName", "lastName", "age", "experience", "almaMater",
    "offensivePreference", "defensivePreference",
    "coachPrestige", "offenseRating", "defenseRating", "recruitingRating", "developmentRating",
    "careerWins", "careerLosses", "championships", "conferenceChampionships",
    "finalFours", "tournamentAppearances",
    "hallOfFamersCoached", "playersDrafted",
    "nationalCoachOfYearCount", "confCoachOfYearCount",
    "yearsAtSchool", "consecutiveMissedTournaments",
    "isHallOfFame", "isWarned", "wearsGlasses",
    "hairStyle", "facialHairStyle",
    "skinTone", "hairColor", "shirtColor", "tieColor",
    "coachingPoints",
]
_COACHES_LOCKED = {"id"}


def _coaches_to_df(coaches: list[dict]) -> pd.DataFrame:
    rows = [{col: c.get(col) for col in _COACHES_DISPLAY_COLS} for c in coaches]
    return pd.DataFrame(rows)


def _coaches_df_to_list(raw: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {row["id"]: row.to_dict() for _, row in df.iterrows()}
    result = []
    for c in raw:
        cid = c.get("id", "")
        if cid in edits:
            merged = dict(c)
            for k, v in edits[cid].items():
                if k in merged:
                    merged[k] = _coerce_val(v, merged[k])
            result.append(merged)
        else:
            result.append(c)
    return result


def _coaches_col_cfg() -> dict:
    pref_options = ["drive", "motion", "highLow", "Princeton", "dribbleDrive", "postalUp", "spread"]
    def_options = ["manToMan", "zone32", "zone23", "zone22", "matchup", "trapping"]
    hair_options = ["short", "medium", "long", "buzz", "afro", "part", "bald", "mohawk", "cornrows"]
    return {
        "id":                    st.column_config.TextColumn("ID", disabled=True),
        "offensivePreference":   st.column_config.SelectboxColumn("Off. Pref", options=pref_options),
        "defensivePreference":   st.column_config.SelectboxColumn("Def. Pref", options=def_options),
        "hairStyle":             st.column_config.SelectboxColumn("Hair Style", options=hair_options),
        "coachPrestige":         st.column_config.NumberColumn("Prestige", min_value=1, max_value=99),
        "offenseRating":         st.column_config.NumberColumn("Off Rtg", min_value=1, max_value=99),
        "defenseRating":         st.column_config.NumberColumn("Def Rtg", min_value=1, max_value=99),
        "recruitingRating":      st.column_config.NumberColumn("Rec Rtg", min_value=1, max_value=99),
        "developmentRating":     st.column_config.NumberColumn("Dev Rtg", min_value=1, max_value=99),
        "isHallOfFame":          st.column_config.CheckboxColumn("HOF"),
        "isWarned":              st.column_config.CheckboxColumn("Warned"),
        "wearsGlasses":          st.column_config.CheckboxColumn("Glasses"),
    }


# ================================================================== players

_PLAYERS_DISPLAY_COLS = [
    "id", "_team_name",
    "firstName", "lastName", "position", "year", "jerseyNumber",
    "teamId", "homeState", "hometown", "highSchool",
    "height", "weight",
    "overallRating", "potentialRating",
    "insideShooting", "midRangeShooting", "outsideShooting",
    "handling", "passing", "rebounding",
    "perimeterDefense", "interiorDefense", "stealing", "blocking",
    "loyalty", "ambition", "playingTimeDesire", "homeAttachment", "morale",
    "isInjured", "isRedshirted", "hasUsedRedshirt", "draftProjection",
]
_PLAYERS_LOCKED = {"id", "teamId", "_team_name", "_team_idx"}
_POSITIONS = ["pointGuard", "shootingGuard", "smallForward", "powerForward", "center"]


def _teams_to_players_df(teams: list[dict]) -> pd.DataFrame:
    rows = []
    for idx, team in enumerate(teams):
        team_name = team.get("teamId") or team.get("id") or str(idx)
        for p in (team.get("players") or []):
            row = {col: p.get(col) for col in _PLAYERS_DISPLAY_COLS
                   if col not in ("_team_name", "_team_idx")}
            row["_team_name"] = team_name
            row["_team_idx"] = idx
            rows.append(row)
    return pd.DataFrame(rows)


def _players_df_to_teams(teams: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {}
    for _, row in df.iterrows():
        pid = row.get("id")
        if pid:
            edits[pid] = row.to_dict()

    result = []
    for team in teams:
        new_players = []
        for p in (team.get("players") or []):
            pid = p.get("id")
            if pid and pid in edits:
                merged = dict(p)
                for k, v in edits[pid].items():
                    if k.startswith("_"):
                        continue
                    if k in merged:
                        merged[k] = _coerce_val(v, merged[k])
                new_players.append(merged)
            else:
                new_players.append(p)
        new_team = dict(team)
        new_team["players"] = new_players
        result.append(new_team)
    return result


def _players_col_cfg() -> dict:
    return {
        "id":           st.column_config.TextColumn("ID", disabled=True),
        "_team_name":   st.column_config.TextColumn("Team", disabled=True),
        "teamId":       st.column_config.TextColumn("Team ID", disabled=True),
        "position":     st.column_config.SelectboxColumn("Position", options=_POSITIONS),
        "year":         st.column_config.NumberColumn("Year", min_value=1, max_value=5),
        "height":       st.column_config.NumberColumn("Ht (in)", min_value=60, max_value=96),
        "weight":       st.column_config.NumberColumn("Wt (lbs)", min_value=100, max_value=400),
        "overallRating":       st.column_config.NumberColumn("OVR", min_value=0, max_value=99),
        "potentialRating":     st.column_config.NumberColumn("POT", min_value=0, max_value=99),
        "insideShooting":      st.column_config.NumberColumn("INS", min_value=0, max_value=99),
        "midRangeShooting":    st.column_config.NumberColumn("MID", min_value=0, max_value=99),
        "outsideShooting":     st.column_config.NumberColumn("OUT", min_value=0, max_value=99),
        "handling":            st.column_config.NumberColumn("HND", min_value=0, max_value=99),
        "passing":             st.column_config.NumberColumn("PAS", min_value=0, max_value=99),
        "rebounding":          st.column_config.NumberColumn("REB", min_value=0, max_value=99),
        "perimeterDefense":    st.column_config.NumberColumn("PDef", min_value=0, max_value=99),
        "interiorDefense":     st.column_config.NumberColumn("IDef", min_value=0, max_value=99),
        "stealing":            st.column_config.NumberColumn("STL", min_value=0, max_value=99),
        "blocking":            st.column_config.NumberColumn("BLK", min_value=0, max_value=99),
        "isInjured":           st.column_config.CheckboxColumn("Injured"),
        "isRedshirted":        st.column_config.CheckboxColumn("RS"),
        "hasUsedRedshirt":     st.column_config.CheckboxColumn("RS Used"),
    }


# ================================================================== conferences

def _conferences_to_df(conferences: list, power_conferences: list) -> pd.DataFrame:
    power_set = set(power_conferences or [])
    return pd.DataFrame([
        {"conference": name, "is_power": name in power_set}
        for name in (conferences or [])
    ])


def _conferences_df_to_lists(df: pd.DataFrame) -> tuple[list, list]:
    all_c = df["conference"].tolist()
    power_c = df[df["is_power"] == True]["conference"].tolist()
    return all_c, power_c


# ================================================================== teams

_TEAMS_SCALAR_COLS = [
    "id", "name", "mascot", "abbreviation", "state",
    "conference", "conferenceId", "isPowerConference",
    "offensiveScheme", "defensiveScheme",
    "prestige", "startingPrestige", "offenseRating", "defenseRating", "expectedWins",
    "teamColor", "secondaryColor", "nilBudget", "isUserControlled", "coachId",
    "wins", "losses", "conferenceWins", "conferenceLosses",
]
_TEAMS_LOCKED = {"id", "wins", "losses", "conferenceWins", "conferenceLosses"}


def _teams_to_df(teams: list[dict]) -> pd.DataFrame:
    rows = []
    for t in teams:
        row = {col: t.get(col) for col in _TEAMS_SCALAR_COLS}
        row["pipelineStates"] = ", ".join(t.get("pipelineStates") or [])
        row["rivalTeamIds"] = ", ".join(str(x) for x in (t.get("rivalTeamIds") or []))
        rows.append(row)
    return pd.DataFrame(rows)


def _teams_df_to_list(raw: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {row["id"]: row.to_dict() for _, row in df.iterrows()}
    result = []
    for t in raw:
        tid = t.get("id", "")
        if tid not in edits:
            result.append(t)
            continue
        merged = dict(t)
        edit = edits[tid]
        for k, v in edit.items():
            if k == "pipelineStates":
                merged["pipelineStates"] = [s.strip() for s in str(v).split(",") if s.strip()] if v else []
            elif k == "rivalTeamIds":
                merged["rivalTeamIds"] = [s.strip() for s in str(v).split(",") if s.strip()] if v else []
            elif k in merged and not isinstance(merged.get(k), (dict, list)):
                merged[k] = _coerce_val(v, merged[k])
        result.append(merged)
    return result


def _teams_col_cfg(conference_options: list[str]) -> dict:
    off_schemes = ["drive", "motion", "highLow", "Princeton", "dribbleDrive", "postalUp", "spread"]
    def_schemes = ["manToMan", "zone32", "zone23", "zone22", "matchup", "trapping"]
    return {
        "id":               st.column_config.TextColumn("ID", disabled=True),
        "conference":       st.column_config.SelectboxColumn("Conference", options=conference_options),
        "offensiveScheme":  st.column_config.SelectboxColumn("Off Scheme", options=off_schemes),
        "defensiveScheme":  st.column_config.SelectboxColumn("Def Scheme", options=def_schemes),
        "isPowerConference":st.column_config.CheckboxColumn("Power"),
        "isUserControlled": st.column_config.CheckboxColumn("User"),
        "prestige":         st.column_config.NumberColumn("Prestige", min_value=1, max_value=99),
        "startingPrestige": st.column_config.NumberColumn("StartPres", min_value=1, max_value=99),
        "offenseRating":    st.column_config.NumberColumn("OffRtg", min_value=1, max_value=99),
        "defenseRating":    st.column_config.NumberColumn("DefRtg", min_value=1, max_value=99),
        "expectedWins":     st.column_config.NumberColumn("ExpW", min_value=0, max_value=50),
        "wins":             st.column_config.NumberColumn("W", disabled=True),
        "losses":           st.column_config.NumberColumn("L", disabled=True),
        "conferenceWins":   st.column_config.NumberColumn("CW", disabled=True),
        "conferenceLosses": st.column_config.NumberColumn("CL", disabled=True),
    }


# ================================================================== download builder

def _apply_all_edits(save: SaveFile):
    """Flush all in-memory DataFrame edits back into save.session."""
    edits = st.session_state.get("page_edits", {})
    raw = st.session_state.get("raw_data", {})

    # Apply conferences first so team conference dropdowns reference a valid list
    if "conferences" in edits:
        all_c, power_c = _conferences_df_to_lists(edits["conferences"])
        save.set("season.conferences", all_c)
        save.set("season.powerConferences", power_c)

    # Teams and players both write to season.teams — apply together
    if "teams" in edits or "players" in edits:
        teams = save.get("season.teams") or []
        if "teams" in edits and "teams" in raw:
            teams = _teams_df_to_list(teams, edits["teams"])
        if "players" in edits:
            teams = _players_df_to_teams(teams, edits["players"])
        save.set("season.teams", teams)

    if "coaches" in edits and "coaches" in raw:
        save.set("season.coaches", _coaches_df_to_list(raw["coaches"], edits["coaches"]))

    if "recruiting" in edits and "recruiting" in raw:
        edits_by_id = {row.get("id"): row.to_dict()
                       for _, row in edits["recruiting"].iterrows() if row.get("id")}
        result = []
        for r in raw["recruiting"]:
            rid = r.get("id")
            if rid and rid in edits_by_id:
                merged = dict(r)
                for k, v in edits_by_id[rid].items():
                    if k in merged:
                        merged[k] = _coerce_val(v, merged[k])
                result.append(merged)
            else:
                result.append(r)
        save.set("season.recruitingPool", result)


def _csv_download_btn(df: pd.DataFrame, filename: str, label: str = "Download CSV"):
    st.download_button(label, df.to_csv(index=False).encode(), filename, "text/csv",
                       use_container_width=True)


def _csv_upload_widget(page_key: str, reference_df: pd.DataFrame, key: str):
    """Show a CSV uploader; on new file upload, store result in page_edits and mark dirty."""
    uploaded_csv = st.file_uploader("Upload CSV to overwrite", type="csv", key=key)
    if uploaded_csv is not None:
        # Only process when the file actually changes (prevents re-applying on every rerun)
        file_id = f"{uploaded_csv.name}_{uploaded_csv.size}"
        last_id_key = f"_csv_last_id_{key}"
        if st.session_state.get(last_id_key) != file_id:
            try:
                new_df = pd.read_csv(uploaded_csv)
                for col in new_df.columns:
                    if col in reference_df.columns:
                        try:
                            new_df[col] = new_df[col].astype(reference_df[col].dtype)
                        except Exception:
                            pass
                st.session_state.setdefault("page_edits", {})[page_key] = new_df
                _mark_dirty(page_key)
                st.session_state[last_id_key] = file_id
                st.success(f"Loaded {len(new_df)} rows from CSV.")
            except Exception as e:
                st.error(f"Could not read CSV: {e}")


# ================================================================== sidebar

page = st.sidebar.radio(
    "Page",
    ["Recruiting Pool", "Coaches", "Rosters", "Teams", "Conferences"],
)

with st.sidebar:
    st.header("Save File")
    uploaded = st.file_uploader("Upload save (.campushoops or .zip)")

    if uploaded:
        if not zipfile.is_zipfile(uploaded):
            st.error("That file doesn't appear to be a valid save export.")
            st.stop()
        uploaded.seek(0)
        raw_zip_bytes = uploaded.read()

        upload_key = f"{uploaded.name}_{uploaded.size}"
        if st.session_state.get("upload_key") != upload_key:
            old_dir = st.session_state.get("temp_dir")
            if old_dir and os.path.exists(old_dir):
                shutil.rmtree(old_dir, ignore_errors=True)

            temp_dir = tempfile.mkdtemp()
            with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as zf:
                zf.extractall(temp_dir)

            save_root = _find_save_root(temp_dir)
            st.session_state["upload_key"] = upload_key
            st.session_state["temp_dir"] = temp_dir
            st.session_state["save"] = SaveFile(save_root)
            st.session_state["raw_zip"] = raw_zip_bytes
            # Clear all edit state on new upload
            st.session_state["page_edits"] = {}
            st.session_state["raw_data"] = {}
            st.session_state["dirty_pages"] = set()
            st.session_state.pop("download_bytes", None)

    if "save" in st.session_state:
        save: SaveFile = st.session_state["save"]
        st.caption(
            f"**{save.meta.get('teamName')}** — Season {save.meta.get('seasonYear')}\n\n"
            f"Last saved: {save.meta.get('lastSaved', '')[:10]}"
        )

        st.divider()

        dirty = st.session_state.get("dirty_pages", set())
        if dirty:
            st.warning(f"Unsaved edits on: {', '.join(sorted(dirty))}. "
                       "Download your file before refreshing.")

        if st.button("Build .campushoops", use_container_width=True,
                     help="Applies all edits and compresses the save file for download."):
            with st.spinner("Building…"):
                _apply_all_edits(save)
                st.session_state["download_bytes"] = save.to_campushoops_bytes(
                    st.session_state["raw_zip"]
                )

        if "download_bytes" in st.session_state:
            team_id = save.meta.get("teamId", "save")
            st.download_button(
                "Download .campushoops",
                st.session_state["download_bytes"],
                file_name=f"modified_{team_id}.campushoops",
                mime="application/zip",
                use_container_width=True,
                type="primary",
            )
    else:
        st.info("Upload your save file to begin.")


# ================================================================== Recruiting Pool

def render_recruiting():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Recruiting Pool")
    save: SaveFile = st.session_state["save"]
    upload_key = st.session_state["upload_key"]

    if "pool" not in st.session_state or st.session_state.get("pool_key") != upload_key:
        raw_pool = save.get("season.recruitingPool") or []
        st.session_state["pool"] = raw_pool
        st.session_state["pool_key"] = upload_key

    pool = RecruitingPool(st.session_state["pool"])
    st.caption(f"{len(pool)} recruits in pool")

    tab_view, tab_edit = st.tabs(["View / Filter", "Edit"])

    # ---- View tab (read-only, with filters) ----
    with tab_view:
        with st.sidebar:
            st.header("Filters")
            selected_positions = st.multiselect("Position", list(POSITION_ABBR.values()))
            rating_range = st.slider("Rating", 0, 99, (0, 99))
            potential_range = st.slider("Potential", 0, 99, (0, 99))
            _hl = [fmt_height(h) for h in range(60, 91)]
            _hs = st.select_slider("Height", options=_hl, value=(fmt_height(60), fmt_height(90)))
            height_range = (60 + _hl.index(_hs[0]), 60 + _hl.index(_hs[1]))
            fc1, fc2 = st.columns(2)
            min_stars = fc1.selectbox("Min ★", [0, 1, 2, 3, 4, 5])
            max_ranking = fc2.number_input("Max rank", min_value=1, value=1600)
            fc3, fc4 = st.columns(2)
            state_filter = fc3.text_input("State", "").upper().strip() or None
            recruit_type = fc4.selectbox("Type", ["All", "HS", "Xfer"])
            type_map = {"All": None, "HS": "highSchool", "Xfer": "transferPortal"}
            fc5, fc6 = st.columns(2)
            scouted_filter = fc5.selectbox("Scouted", ["All", "Yes", "No"])
            scouted_map = {"All": None, "Yes": True, "No": False}
            sort_by = fc6.selectbox("Sort by", [
                "rating", "potential", "ranking", "generatedStarRating", "height",
                "insideShooting", "midRangeShooting", "outsideShooting",
                "handling", "passing", "rebounding",
                "perimeterDefense", "interiorDefense", "stealing", "blocking",
            ])

        filtered = pool.filter(
            position=selected_positions if selected_positions else None,
            min_rating=rating_range[0], max_rating=rating_range[1],
            min_potential=potential_range[0], max_potential=potential_range[1],
            min_height=height_range[0], max_height=height_range[1],
            min_stars=min_stars, max_ranking=max_ranking,
            state=state_filter,
            recruit_type=type_map[recruit_type],
            is_scouted=scouted_map[scouted_filter],
        )
        filtered_sorted = sorted(filtered, key=lambda r: r.get(sort_by) or 0, reverse=True)
        df_view = pool.to_df(filtered_sorted)
        st.caption(f"Showing {len(df_view)} recruits")

        col_cfg = {
            "height": st.column_config.TextColumn(
                "Height", help="Use 'Sort by → height' sidebar for correct ordering"),
            "stars": st.column_config.NumberColumn("Stars", format="%d ⭐"),
        }
        core_cols = ["name", "pos", "stars", "rating", "potential", "ranking",
                     "pos_rank", "height", "weight", "state", "hometown", "type",
                     "scouted", "late_bloomer", "generational", "schools", "awards"]
        skill_cols = ["inside", "mid", "outside", "handling", "passing",
                      "rebounding", "perim_def", "int_def", "stealing", "blocking"]
        personality_cols = ["loyalty", "ambition", "playing_time_desire", "home_attachment"]

        sub1, sub2, sub3 = st.tabs(["Overview", "Skills", "Personality"])
        with sub1:
            st.dataframe(df_view[core_cols], column_config=col_cfg,
                         use_container_width=True, hide_index=True)
        with sub2:
            st.dataframe(df_view[["name", "pos", "rating", "height"] + skill_cols],
                         column_config=col_cfg, use_container_width=True, hide_index=True)
        with sub3:
            st.info(
                "**Personality as recruiting propensity**\n\n"
                "- **Loyalty** — prefers staying close to home\n"
                "- **Home Attachment** — weights distance from hometown heavily\n"
                "- **Playing Time Desire** — will prioritize where he's most likely to start\n"
                "- **Ambition** — chases prestige; easier to flip toward higher-ranked programs"
            )
            st.dataframe(df_view[["name", "pos", "rating"] + personality_cols],
                         use_container_width=True, hide_index=True)

    # ---- Edit tab ----
    with tab_edit:
        page_key = "recruiting"
        raw_pool = st.session_state["pool"]

        if page_key not in st.session_state.get("page_edits", {}):
            # Build editable df from raw pool (scalar fields only)
            skip = {"interestedSchools", "awards", "seasonLogs", "startOfSeasonAttributes"}
            sample = raw_pool[0] if raw_pool else {}
            edit_cols = [k for k, v in sample.items()
                         if not isinstance(v, (dict, list)) and k not in skip]
            rows = [{c: r.get(c) for c in edit_cols} for r in raw_pool]
            edit_df = pd.DataFrame(rows)
            st.session_state.setdefault("page_edits", {})[page_key] = edit_df
            st.session_state.setdefault("raw_data", {})[page_key] = raw_pool

        current_df = st.session_state["page_edits"][page_key]
        locked = {c for c in current_df.columns if c.lower().endswith("id") or c == "id"}
        col_cfg_edit = {c: st.column_config.Column(disabled=True) for c in locked}

        edited = st.data_editor(
            current_df,
            column_config=col_cfg_edit,
            use_container_width=True,
            num_rows="dynamic",
            key=f"editor_{page_key}",
        )

        if not edited.equals(current_df):
            st.session_state["page_edits"][page_key] = edited
            _mark_dirty(page_key)

        c1, c2 = st.columns(2)
        with c1:
            _csv_download_btn(edited, "recruiting_pool.csv")
        with c2:
            _csv_upload_widget(page_key, current_df, key="csv_up_recruiting")


# ================================================================== Coaches

def render_coaches():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Coaches")
    save: SaveFile = st.session_state["save"]
    page_key = "coaches"

    if page_key not in st.session_state.get("page_edits", {}):
        raw = save.get("season.coaches") or []
        st.session_state.setdefault("raw_data", {})[page_key] = raw
        st.session_state.setdefault("page_edits", {})[page_key] = _coaches_to_df(raw)

    current_df = st.session_state["page_edits"][page_key]
    st.caption(f"{len(current_df)} coaches")

    col_cfg = _coaches_col_cfg()
    for c in _COACHES_LOCKED:
        col_cfg[c] = st.column_config.TextColumn(c, disabled=True)

    edited = st.data_editor(
        current_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="dynamic",
        key="editor_coaches",
    )

    if not edited.equals(current_df):
        st.session_state["page_edits"][page_key] = edited
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        _csv_download_btn(edited, "coaches.csv")
    with c2:
        _csv_upload_widget(page_key, current_df, key="csv_up_coaches")


# ================================================================== Rosters

def render_rosters():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Rosters")
    save: SaveFile = st.session_state["save"]
    page_key = "players"

    if page_key not in st.session_state.get("page_edits", {}):
        teams = save.get("season.teams") or []
        full_df = _teams_to_players_df(teams)
        st.session_state.setdefault("page_edits", {})[page_key] = full_df

    full_df: pd.DataFrame = st.session_state["page_edits"][page_key]

    # Ensure meta columns exist (e.g. after CSV upload without them)
    if "_team_name" not in full_df.columns:
        full_df["_team_name"] = full_df.get("teamId", "unknown")
        st.session_state["page_edits"][page_key] = full_df
    if "_team_idx" not in full_df.columns:
        full_df["_team_idx"] = 0
        st.session_state["page_edits"][page_key] = full_df

    team_names = sorted(full_df["_team_name"].dropna().unique().tolist())
    view_options = ["All Teams"] + team_names
    selected = st.selectbox("Team", view_options)

    if selected == "All Teams":
        display_df = full_df
    else:
        display_df = full_df[full_df["_team_name"] == selected].copy()

    st.caption(f"{len(display_df)} players" + ("" if selected == "All Teams" else f" on {selected}"))

    col_cfg = _players_col_cfg()

    edited_display = st.data_editor(
        display_df.drop(columns=["_team_idx"], errors="ignore"),
        column_config=col_cfg,
        use_container_width=True,
        num_rows="dynamic",
        key=f"editor_players_{selected}",
    )

    # Merge edits back into the full DataFrame
    if not edited_display.equals(display_df.drop(columns=["_team_idx"], errors="ignore")):
        id_to_edit = edited_display.set_index("id").to_dict("index")
        new_full = full_df.copy()
        for idx, row in new_full.iterrows():
            pid = row.get("id")
            if pid and pid in id_to_edit:
                for col, val in id_to_edit[pid].items():
                    new_full.at[idx, col] = val
        st.session_state["page_edits"][page_key] = new_full
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        fname = f"roster_{selected.replace(' ', '_')}.csv" if selected != "All Teams" else "all_players.csv"
        _csv_download_btn(edited_display, fname)
    with c2:
        _csv_upload_widget(page_key, full_df, key=f"csv_up_players_{selected}")


# ================================================================== Teams

def render_teams():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Teams")
    save: SaveFile = st.session_state["save"]
    page_key = "teams"

    if page_key not in st.session_state.get("page_edits", {}):
        raw = save.get("season.teams") or []
        st.session_state.setdefault("raw_data", {})[page_key] = raw
        st.session_state.setdefault("page_edits", {})[page_key] = _teams_to_df(raw)

    current_df: pd.DataFrame = st.session_state["page_edits"][page_key]

    # Use live conference list (may have been edited on Conferences page)
    conf_edits = st.session_state.get("page_edits", {}).get("conferences")
    if conf_edits is not None:
        all_conferences = sorted(conf_edits["conference"].dropna().tolist())
    else:
        all_conferences = sorted(save.get("season.conferences") or [])

    col_cfg = _teams_col_cfg(all_conferences)

    conf_filter = st.selectbox("Filter by conference", ["All Teams"] + all_conferences)
    if conf_filter != "All Teams":
        display_df = current_df[current_df["conference"] == conf_filter].copy()
    else:
        display_df = current_df

    st.caption(
        f"{len(display_df)} team(s) shown — edit conference assignments, schemes, prestige, and colors."
    )

    edited = st.data_editor(
        display_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="fixed",
        key=f"editor_teams_{conf_filter}",
    )

    # Merge edits back into full DataFrame
    if not edited.equals(display_df):
        id_to_edit = edited.set_index("id").to_dict("index")
        new_full = current_df.copy()
        for idx, row in new_full.iterrows():
            tid = row.get("id")
            if tid and tid in id_to_edit:
                for col, val in id_to_edit[tid].items():
                    new_full.at[idx, col] = val
        st.session_state["page_edits"][page_key] = new_full
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        fname = f"teams_{conf_filter.replace(' ', '_')}.csv"
        _csv_download_btn(edited, fname)
    with c2:
        _csv_upload_widget(page_key, current_df, key=f"csv_up_teams_{conf_filter}")


# ================================================================== Conferences

def render_conferences():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Conferences")
    save: SaveFile = st.session_state["save"]
    page_key = "conferences"

    if page_key not in st.session_state.get("page_edits", {}):
        conferences = save.get("season.conferences") or []
        power = save.get("season.powerConferences") or []
        df = _conferences_to_df(conferences, power)
        st.session_state.setdefault("page_edits", {})[page_key] = df

    current_df = st.session_state["page_edits"][page_key]
    st.caption(f"{len(current_df)} conferences — edit names or toggle power conference status.")

    col_cfg = {
        "conference": st.column_config.TextColumn("Conference Name"),
        "is_power":   st.column_config.CheckboxColumn("Power Conference"),
    }

    edited = st.data_editor(
        current_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="dynamic",
        key="editor_conferences",
    )

    if not edited.equals(current_df):
        st.session_state["page_edits"][page_key] = edited
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        _csv_download_btn(edited, "conferences.csv")
    with c2:
        _csv_upload_widget(page_key, current_df, key="csv_up_conferences")


# ================================================================== Leaderboard (hidden from nav)

def _fmt_playtime(seconds: int | None) -> str:
    if not seconds:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def render_leaderboard():
    st.header("Leaderboard")
    try:
        challenges = db.get_active_challenges()
    except Exception as e:
        st.error(f"Could not load challenges: {e}")
        return
    if not challenges:
        st.info("No challenges yet.")
        return
    challenge_names = [c["name"] for c in challenges]
    selected_name = st.selectbox("Select a challenge", challenge_names)
    challenge = next(c for c in challenges if c["name"] == selected_name)
    st.markdown(f"**{challenge['name']}**")
    if challenge.get("description"):
        st.caption(challenge["description"])
    with st.expander("Challenge conditions"):
        for key, val in challenge.get("conditions", {}).items():
            st.markdown(f"- **{vf.CONDITION_LABELS.get(key, key)}**: {val}")
    st.divider()
    entries = db.get_leaderboard(challenge["id"])
    if not entries:
        st.info("No verified submissions yet.")
        return
    rows = []
    for i, e in enumerate(entries, 1):
        w, l = e.get("career_wins") or 0, e.get("career_losses") or 0
        rows.append({
            "#": i, "Username": e["username"], "Coach": e.get("coach_name") or "—",
            "Team": e["team_name"], "Seasons": e.get("seasons_played") or "—",
            "W": w, "L": l,
            "Win %": f"{w / (w + l) * 100:.1f}%" if (w + l) > 0 else "—",
            "Play Time": _fmt_playtime(e.get("play_time_seconds")),
            "Submitted": str(e.get("submitted_at", ""))[:10],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ================================================================== Submit (hidden)

def render_submit():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return
    st.header("Submit to Challenge")
    save: SaveFile = st.session_state["save"]
    try:
        challenges = db.get_active_challenges()
    except Exception as e:
        st.error(f"Could not load challenges: {e}")
        return
    if not challenges:
        st.info("No active challenges.")
        return
    username = st.text_input("Your leaderboard username", max_chars=32)
    challenge = next(c for c in challenges
                     if c["name"] == st.selectbox("Challenge", [c["name"] for c in challenges]))
    if challenge.get("description"):
        st.caption(challenge["description"])
    if not username.strip():
        st.warning("Enter a username to continue.")
        return
    if not _is_clean(username):
        st.error("Please choose an appropriate username.")
        return
    st.divider()
    conditions = challenge.get("conditions", {})
    results = vf.verify(save, conditions)
    career = vf.get_career_stats(save)
    all_ok = vf.all_passed(results)
    for key, r in results.items():
        icon = "✅" if r["passed"] else "❌"
        st.markdown(f"{icon} **{r['label']}** — {r['detail']}")
    st.divider()
    if not all_ok:
        st.error("Save does not meet all challenge conditions.")
        return
    st.success("All conditions passed!")
    st.caption(f"Coach: **{career['coach_name']}** — {career['seasons_played']} seasons — "
               f"{career['career_wins']}W / {career['career_losses']}L ({career['win_pct']}%)")
    if st.button("Submit to Leaderboard", type="primary"):
        db.upsert_submission(
            username=username.strip(), challenge_id=challenge["id"],
            team_name=save.meta.get("teamName", ""), team_id=save.meta.get("teamId", ""),
            seasons_played=career["seasons_played"],
            play_time_seconds=save.meta.get("playTimeSeconds", 0),
            verified=True, conditions_met=results,
            coach_name=career["coach_name"],
            career_wins=career["career_wins"], career_losses=career["career_losses"],
        )
        st.success("Submitted!")
        st.balloons()


# ================================================================== Create Challenge (hidden)

def render_create_challenge():
    st.header("Create a Challenge")
    creator = st.text_input("Your name / username", max_chars=32)
    name = st.text_input("Challenge name", max_chars=80)
    description = st.text_area("Description", max_chars=500)
    st.divider()
    st.subheader("Conditions")
    if "draft_conditions" not in st.session_state:
        st.session_state["draft_conditions"] = {}
    conditions = st.session_state["draft_conditions"]
    _BOOL = {"must_win_championship", "must_make_tournament", "must_make_final_four", "single_team_only"}
    _TEXT = {"start_team_id"}
    _PRESTIGE = {"max_start_prestige", "max_recruit_rating"}
    _COUNT = {"min_championships", "min_tournament_appearances", "max_seasons"}
    with st.expander("Add a condition", expanded=not conditions):
        ctype = st.selectbox("Condition type", list(vf.CONDITION_LABELS.keys()),
                             format_func=lambda k: vf.CONDITION_LABELS[k])
        st.caption(vf.CONDITION_HELP.get(ctype, ""))
        if ctype in _BOOL:
            cval = True
            st.info("This condition is either required or not — add it to require it.")
        elif ctype in _TEXT:
            cval = st.text_input("Team ID", placeholder="saint_francis_pa")
        elif ctype in _PRESTIGE:
            cval = st.slider("Value", min_value=1, max_value=99, value=50)
        elif ctype in _COUNT:
            cval = st.number_input("Value", min_value=1, value=1)
        else:
            cval = None
        if st.button("Add condition"):
            if cval not in (None, "", 0):
                conditions[ctype] = cval
                st.session_state["draft_conditions"] = conditions
                st.rerun()
    if conditions:
        st.markdown("**Current conditions:**")
        to_remove = None
        for key, val in conditions.items():
            col_l, col_r = st.columns([5, 1])
            col_l.markdown(f"- **{vf.CONDITION_LABELS.get(key, key)}**: {val}")
            if col_r.button("Remove", key=f"rm_{key}"):
                to_remove = key
        if to_remove:
            del conditions[to_remove]
            st.rerun()
    else:
        st.info("No conditions added — fully honor system.")
    st.divider()
    if not (creator.strip() and name.strip()):
        st.warning("Enter your name and challenge name to publish.")
        return
    if not all(_is_clean(t) for t in [creator, name, description]):
        st.error("Please keep content appropriate.")
        return
    if st.button("Publish Challenge", type="primary"):
        try:
            db.create_challenge(created_by=creator.strip(), name=name.strip(),
                                description=description.strip(), conditions=conditions)
            st.session_state["draft_conditions"] = {}
            st.success(f"Challenge '{name}' published!")
        except Exception as e:
            st.error(f"Failed to publish: {e}")


# ================================================================== router

if page == "Recruiting Pool":
    render_recruiting()
elif page == "Coaches":
    render_coaches()
elif page == "Rosters":
    render_rosters()
elif page == "Teams":
    render_teams()
elif page == "Conferences":
    render_conferences()
