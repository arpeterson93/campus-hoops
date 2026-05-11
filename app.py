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
from better_profanity import profanity as _profanity

import database as db
import verifier as vf
from recruiting import POSITION_ABBR, RecruitingPool, fmt_height
from save_loader import SaveFile

_profanity.load_censor_words()

st.set_page_config(page_title="Campus Hoops Utility", layout="wide")
st.title("Campus Hoops Utility")


def _is_clean(text: str) -> bool:
    return not _profanity.contains_profanity(text)


# ================================================================== shared: file upload

def _find_save_root(extracted_dir: str) -> str:
    contents = os.listdir(extracted_dir)
    if len(contents) == 1:
        candidate = os.path.join(extracted_dir, contents[0])
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "meta.json")):
            return candidate
    return extracted_dir


# ================================================================== page nav (always visible)

page = st.sidebar.radio(
    "Page",
    ["Recruiting Pool", "Leaderboard", "Submit to Challenge", "Create Challenge"],
)

# ================================================================== save file upload (required only for some pages)

SAVE_REQUIRED_PAGES = {"Recruiting Pool", "Submit to Challenge"}

with st.sidebar:
    st.header("Save File")
    uploaded = st.file_uploader("Upload save file (.campushoops or .zip)")

    if uploaded:
        if not zipfile.is_zipfile(uploaded):
            st.error("That file doesn't appear to be a valid save export.")
            st.stop()
        uploaded.seek(0)

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
            st.session_state.pop("pool", None)

    if "save" in st.session_state:
        save: SaveFile = st.session_state["save"]
        st.caption(
            f"**{save.meta.get('teamName')}** — Season {save.meta.get('seasonYear')}\n\n"
            f"Last saved: {save.meta.get('lastSaved', '')[:10]}"
        )
    elif page in SAVE_REQUIRED_PAGES:
        st.info("Upload your save file (.campushoops or .zip) to use this page.")
        st.stop()


# ================================================================== Recruiting Pool

def render_recruiting():
    st.header("Recruiting Pool")
    save: SaveFile = st.session_state["save"]
    upload_key = st.session_state["upload_key"]

    if "pool" not in st.session_state or st.session_state.get("pool_key") != upload_key:
        raw_pool = save.get("season.recruitingPool") or []
        st.session_state["pool"] = raw_pool
        st.session_state["pool_key"] = upload_key

    pool = RecruitingPool(st.session_state["pool"])
    st.caption(f"{len(pool)} recruits in pool")

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

    filtered = pool.filter(
        position=selected_positions if selected_positions else None,
        min_rating=min_rating, max_rating=max_rating,
        min_potential=min_potential, max_potential=max_potential,
        min_height=height_range[0], max_height=height_range[1],
        min_stars=min_stars, max_ranking=max_ranking,
        state=state_filter,
        recruit_type=type_map[recruit_type],
        is_scouted=scouted_map[scouted_filter],
    )

    filtered_sorted = sorted(filtered, key=lambda r: r.get(sort_by) or 0, reverse=True)
    df = pool.to_df(filtered_sorted)
    st.caption(f"Showing {len(df)} recruits")

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
            "- **Loyalty** — prefers staying close to home\n"
            "- **Home Attachment** — weights distance from hometown heavily\n"
            "- **Playing Time Desire** — will prioritize where he's most likely to start\n"
            "- **Ambition** — chases prestige; easier to flip toward higher-ranked programs\n\n"
            "The `interest` column reflects recruiting points you've assigned in-game, "
            "not innate propensity."
        )
        st.dataframe(df[["name", "pos", "rating"] + personality_cols],
                     use_container_width=True, hide_index=True)

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


# ================================================================== Leaderboard

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
        st.info("No challenges yet. Be the first to create one!")
        return

    challenge_names = [c["name"] for c in challenges]
    selected_name = st.selectbox("Select a challenge", challenge_names)
    challenge = next(c for c in challenges if c["name"] == selected_name)

    st.markdown(f"**{challenge['name']}**")
    if challenge.get("description"):
        st.caption(challenge["description"])

    with st.expander("Challenge conditions"):
        conditions = challenge.get("conditions", {})
        for key, val in conditions.items():
            label = vf.CONDITION_LABELS.get(key, key)
            st.markdown(f"- **{label}**: {val}")

    st.divider()

    entries = db.get_leaderboard(challenge["id"])

    if not entries:
        st.info("No verified submissions yet for this challenge.")
        return

    import pandas as pd
    rows = []
    for i, e in enumerate(entries, 1):
        w = e.get("career_wins") or 0
        l = e.get("career_losses") or 0
        win_pct = f"{w / (w + l) * 100:.1f}%" if (w + l) > 0 else "—"
        rows.append({
            "Rank": i,
            "Username": e["username"],
            "Coach": e.get("coach_name") or "—",
            "Team": e["team_name"],
            "Season": e["season_year"],
            "W": w,
            "L": l,
            "Win %": win_pct,
            "Play Time": _fmt_playtime(e.get("play_time_seconds")),
            "Submitted": str(e.get("submitted_at", ""))[:10],
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ================================================================== Submit to Challenge

def render_submit():
    st.header("Submit to Challenge")
    save: SaveFile = st.session_state["save"]
    st.caption(
        f"Submitting save: **{save.meta.get('teamName')}** — "
        f"Season {save.meta.get('seasonYear')}"
    )

    try:
        challenges = db.get_active_challenges()
    except Exception as e:
        st.error(f"Could not load challenges: {e}")
        return

    if not challenges:
        st.info("No active challenges to submit to.")
        return

    username = st.text_input("Your leaderboard username", max_chars=32)

    challenge_names = [c["name"] for c in challenges]
    selected_name = st.selectbox("Choose a challenge", challenge_names)
    challenge = next(c for c in challenges if c["name"] == selected_name)

    if challenge.get("description"):
        st.caption(challenge["description"])

    if not username.strip():
        st.warning("Enter a username to continue.")
        return

    if not _is_clean(username):
        st.error("Please choose an appropriate username.")
        return

    st.divider()
    st.subheader("Verification")
    st.caption("The following conditions will be checked against your save:")

    conditions = challenge.get("conditions", {})
    results = vf.verify(save, conditions)
    career = vf.get_career_stats(save)

    all_ok = vf.all_passed(results)
    for key, r in results.items():
        icon = "✅" if r["passed"] else "❌"
        st.markdown(f"{icon} **{r['label']}** — {r['detail']}")

    st.divider()

    if not all_ok:
        st.error("Your save does not meet all challenge conditions. Not eligible to submit.")
        return

    st.success("All conditions passed! Ready to submit.")
    st.caption(
        f"Coach: **{career['coach_name']}** — "
        f"{career['career_wins']}W / {career['career_losses']}L "
        f"({career['win_pct']}%)"
    )

    existing = db.get_leaderboard(challenge["id"])
    existing_entry = next((e for e in existing if e["username"] == username.strip()), None)
    if existing_entry:
        st.warning(
            f"You already have an entry for this challenge (Season {existing_entry['season_year']}). "
            "Submitting will overwrite it."
        )

    if st.button("Submit to Leaderboard", type="primary"):
        db.upsert_submission(
            username=username.strip(),
            challenge_id=challenge["id"],
            team_name=save.meta.get("teamName", ""),
            team_id=save.meta.get("teamId", ""),
            season_year=save.meta.get("seasonYear", 0),
            play_time_seconds=save.meta.get("playTimeSeconds", 0),
            verified=True,
            conditions_met=results,
            coach_name=career["coach_name"],
            career_wins=career["career_wins"],
            career_losses=career["career_losses"],
        )
        st.success("Submitted! Check the Leaderboard page to see your entry.")
        st.balloons()


# ================================================================== Create Challenge

def render_create_challenge():
    st.header("Create a Challenge")
    st.caption(
        "Define a challenge with auto-verifiable conditions. "
        "Other players will submit their saves and be checked against these rules."
    )

    creator = st.text_input("Your name / username", max_chars=32)
    name = st.text_input("Challenge name", max_chars=80, placeholder="Rags to Riches")
    description = st.text_area(
        "Description",
        placeholder="Start with a low-prestige team and win the national championship.",
        max_chars=500,
    )

    st.divider()
    st.subheader("Conditions")
    st.caption(
        "Add conditions that will be automatically verified against each submission's save file. "
        "Anything not listed here is honor system — describe it in the challenge description."
    )

    if "draft_conditions" not in st.session_state:
        st.session_state["draft_conditions"] = {}

    conditions: dict = st.session_state["draft_conditions"]

    # -- add a condition
    # Which condition types take which input widget
    _BOOL_CONDITIONS = {
        "must_win_championship", "must_make_tournament",
        "must_make_final_four", "single_team_only",
    }
    _TEXT_CONDITIONS = {"start_team_id"}
    _NUMBER_CONDITIONS_PRESTIGE = {"max_start_prestige", "max_recruit_rating"}  # 1–99
    _NUMBER_CONDITIONS_COUNT = {
        "min_championships", "min_tournament_appearances", "max_seasons",
    }  # 1+

    with st.expander("Add a condition", expanded=not conditions):
        ctype = st.selectbox(
            "Condition type",
            list(vf.CONDITION_LABELS.keys()),
            format_func=lambda k: vf.CONDITION_LABELS[k],
        )
        st.caption(vf.CONDITION_HELP.get(ctype, ""))

        if ctype in _BOOL_CONDITIONS:
            cval = True
            st.info("This condition is either required or not — add it to require it.")
        elif ctype in _TEXT_CONDITIONS:
            cval = st.text_input("Team ID", placeholder="saint_francis_pa")
        elif ctype in _NUMBER_CONDITIONS_PRESTIGE:
            cval = st.slider("Value", min_value=1, max_value=99, value=50)
        elif ctype in _NUMBER_CONDITIONS_COUNT:
            cval = st.number_input("Value", min_value=1, value=1)
        else:
            cval = None

        if st.button("Add condition"):
            if cval not in (None, "", 0):
                conditions[ctype] = cval
                st.session_state["draft_conditions"] = conditions
                st.rerun()

    # -- show current conditions
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
            st.session_state["draft_conditions"] = conditions
            st.rerun()
    else:
        st.info("No conditions added yet. A challenge with no conditions is fully honor system.")

    st.divider()

    ready = creator.strip() and name.strip()
    if not ready:
        st.warning("Enter your name and a challenge name to publish.")
        return

    if not _is_clean(creator) or not _is_clean(name) or not _is_clean(description):
        st.error("Please keep challenge content appropriate.")
        return

    if st.button("Publish Challenge", type="primary"):
        try:
            db.create_challenge(
                created_by=creator.strip(),
                name=name.strip(),
                description=description.strip(),
                conditions=conditions,
            )
            st.session_state["draft_conditions"] = {}
            st.success(f"Challenge '{name}' published! It will appear on the Leaderboard page.")
        except Exception as e:
            st.error(f"Failed to publish: {e}")


# ================================================================== router

if page == "Recruiting Pool":
    render_recruiting()
elif page == "Leaderboard":
    render_leaderboard()
elif page == "Submit to Challenge":
    render_submit()
elif page == "Create Challenge":
    render_create_challenge()
