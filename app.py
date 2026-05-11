"""
Campus Hoops Utility — Streamlit UI

Run with:
    streamlit run app.py
"""

import os
import shutil
import tempfile
import zipfile

import streamlit as st

from recruiting import POSITION_ABBR, RecruitingPool, fmt_height
from save_loader import SaveFile

st.set_page_config(page_title="Campus Hoops Utility", layout="wide")
st.title("Campus Hoops Utility")

# ------------------------------------------------------------------ file upload

def _find_save_root(extracted_dir: str) -> str:
    """If the zip contained a single folder, step into it; otherwise use root."""
    contents = os.listdir(extracted_dir)
    if len(contents) == 1:
        candidate = os.path.join(extracted_dir, contents[0])
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "meta.json")):
            return candidate
    return extracted_dir


with st.sidebar:
    st.header("Save File")
    uploaded = st.file_uploader("Upload save file (.campushoops or .zip)")

    if not uploaded:
        st.info("Upload your exported save file (.campushoops or .zip) to get started.")
        st.stop()

    if not zipfile.is_zipfile(uploaded):
        st.error("That file doesn't appear to be a valid save export. Please upload a .campushoops or .zip file.")
        st.stop()
    uploaded.seek(0)

    # Re-extract only when a new file is uploaded
    upload_key = f"{uploaded.name}_{uploaded.size}"
    if st.session_state.get("upload_key") != upload_key:
        old_dir = st.session_state.get("temp_dir")
        if old_dir and os.path.exists(old_dir):
            shutil.rmtree(old_dir, ignore_errors=True)

        temp_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(uploaded) as zf:
            zf.extractall(temp_dir)

        save_root = _find_save_root(temp_dir)
        st.session_state["upload_key"] = upload_key
        st.session_state["temp_dir"] = temp_dir
        st.session_state["save"] = SaveFile(save_root)

save: SaveFile = st.session_state["save"]

# ------------------------------------------------------------------ page nav

page = st.sidebar.radio("Page", ["Recruiting Pool"])  # more pages added later

# ================================================================== Recruiting Pool
if page == "Recruiting Pool":
    st.header("Recruiting Pool")

    # session.json is lazy-loaded; pool is cached in session_state after first access
    if "pool" not in st.session_state or st.session_state.get("pool_key") != upload_key:
        raw_pool = save.get("season.recruitingPool") or []
        st.session_state["pool"] = raw_pool
        st.session_state["pool_key"] = upload_key

    pool = RecruitingPool(st.session_state["pool"])
    st.caption(f"{len(pool)} recruits in pool")

    # ---------------------------------------------------------------- filters
    with st.sidebar:
        st.header("Filters")

        selected_positions = st.multiselect("Position", list(POSITION_ABBR.values()))

        rating_range = st.slider("Rating", 0, 99, (0, 99))
        min_rating, max_rating = rating_range

        potential_range = st.slider("Potential", 0, 99, (0, 99))
        min_potential, max_potential = potential_range

        _height_labels = [fmt_height(h) for h in range(60, 91)]
        _height_sel = st.select_slider(
            "Height",
            options=_height_labels,
            value=(fmt_height(60), fmt_height(90)),
        )
        height_range = (
            60 + _height_labels.index(_height_sel[0]),
            60 + _height_labels.index(_height_sel[1]),
        )

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

    # ---------------------------------------------------------------- apply filters
    filtered = pool.filter(
        position=selected_positions if selected_positions else None,
        min_rating=min_rating,
        max_rating=max_rating,
        min_potential=min_potential,
        max_potential=max_potential,
        min_height=height_range[0],
        max_height=height_range[1],
        min_stars=min_stars,
        max_ranking=max_ranking,
        state=state_filter,
        recruit_type=type_map[recruit_type],
        is_scouted=scouted_map[scouted_filter],
    )

    filtered_sorted = sorted(filtered, key=lambda r: r.get(sort_by) or 0, reverse=True)
    df = pool.to_df(filtered_sorted)

    st.caption(f"Showing {len(df)} recruits")

    # ---------------------------------------------------------------- display
    col_cfg = {
        "height": st.column_config.TextColumn(
            "Height",
            help="Use 'Sort by → height' in the sidebar for correct height ordering",
        ),
        "stars": st.column_config.NumberColumn("Stars", format="%d ⭐"),
    }

    core_cols = ["name", "pos", "stars", "rating", "potential", "ranking",
                 "pos_rank", "height", "weight", "state", "hometown", "type",
                 "scouted", "late_bloomer", "generational", "schools", "awards"]

    skill_cols = ["inside", "mid", "outside", "handling", "passing",
                  "rebounding", "perim_def", "int_def", "stealing", "blocking"]

    personality_cols = ["loyalty", "ambition", "playing_time_desire", "home_attachment"]

    tab1, tab2, tab3 = st.tabs(["Overview", "Skills", "Personality"])

    with tab1:
        st.dataframe(df[core_cols], column_config=col_cfg, use_container_width=True, hide_index=True)

    with tab2:
        st.dataframe(df[["name", "pos", "rating", "height"] + skill_cols],
                     column_config=col_cfg, use_container_width=True, hide_index=True)

    with tab3:
        st.info(
            "**Personality as recruiting propensity**\n\n"
            "- **Loyalty** — prefers staying close to home; harder to pull away\n"
            "- **Home Attachment** — weights distance from hometown heavily\n"
            "- **Playing Time Desire** — will prioritize where he's most likely to start\n"
            "- **Ambition** — chases prestige; easier to flip toward higher-ranked programs\n\n"
            "The `interest` column reflects recruiting points you've already assigned in-game, "
            "not innate propensity."
        )
        st.dataframe(df[["name", "pos", "rating"] + personality_cols],
                     use_container_width=True, hide_index=True)

    # ---------------------------------------------------------------- recruit detail
    st.divider()
    st.subheader("Recruit Detail")
    selected_name = st.selectbox("Select a recruit", df["name"].tolist())
    if selected_name:
        match = next((r for r in filtered_sorted
                      if f"{r.get('firstName', '')} {r.get('lastName', '')}".strip() == selected_name), None)
        if match:
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                st.metric("Rating", match.get("rating"))
                st.metric("Potential", match.get("potential"))
                st.metric("Stars", "⭐" * (match.get("generatedStarRating") or 0))
                st.metric("National Rank", match.get("ranking"))
                st.metric("Position Rank", match.get("positionRanking"))
            with col_b:
                st.metric("Position", match.get("position"))
                st.metric("Height", fmt_height(match.get("height")))
                st.metric("Weight", f"{match.get('weight')} lbs")
                st.metric("Hometown", f"{match.get('hometown')}, {match.get('homeState')}")
                st.metric("Schools Interested", len(match.get("interestedSchools") or []))
            with col_c:
                st.metric("Loyalty", match.get("loyalty"))
                st.metric("Ambition", match.get("ambition"))
                st.metric("Playing Time Desire", match.get("playingTimeDesire"))
                st.metric("Home Attachment", match.get("homeAttachment"))
                st.metric("Your Interest", match.get("interest"))
            if match.get("hsAward"):
                st.write(f"**Awards:** {match.get('hsAward')}")
            col_flags = st.columns(3)
            if match.get("isLateBloomer"):
                col_flags[0].info("Late Bloomer")
            if match.get("isGenerational"):
                col_flags[1].success("Generational Talent")
            if match.get("isTransferPortal"):
                col_flags[2].warning("Transfer Portal")
