import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from irctc_agent_alert import (
    Config,
    alert_condition_met,
    clear_last_alert,
    fetch_availability,
    is_cutoff_reached,
    read_last_alert,
    resolve_tracking_url,
    send_pushover_alert,
    state_signature,
    summarize_entries,
    write_last_alert,
)


DEFAULT_STATE_DIR = Path(".alert_state")
DEFAULT_TRACKING_URL = "https://www.confirmtkt.com"
DEFAULT_TRAVEL_DATE = (datetime.now() + timedelta(days=1)).strftime("%d-%m-%Y")


st.set_page_config(page_title="IRCTC Current Availability", page_icon="🚆", layout="wide")


def init_state() -> None:
    st.session_state.setdefault("queries", [])
    st.session_state.setdefault("monitoring_enabled", True)
    st.session_state.setdefault("run_once_all", False)
    st.session_state.setdefault("add_source", "GWL")
    st.session_state.setdefault("add_destination", "NDLS")
    st.session_state.setdefault("add_travel_date", DEFAULT_TRAVEL_DATE)
    st.session_state.setdefault("add_travel_class", "SL")
    st.session_state.setdefault("add_quota", "GN")
    st.session_state.setdefault("add_tracking_url", DEFAULT_TRACKING_URL)
    st.session_state.setdefault("add_train_number", "NA")
    st.session_state.setdefault("add_pushover_user_key", os.getenv("PUSHOVER_USER_KEY", ""))
    st.session_state.setdefault("add_pushover_app_token", os.getenv("PUSHOVER_APP_TOKEN", ""))
    st.session_state.setdefault("add_pushover_device", os.getenv("PUSHOVER_DEVICE", ""))
    st.session_state.setdefault("add_timeout_seconds", 20)
    st.session_state.setdefault("add_browser_wait_seconds", 7)
    st.session_state.setdefault("add_use_browser_rendering", True)
    st.session_state.setdefault("add_cutoff", "")


def normalize_tracking_url(value: str) -> str:
    value = value.strip()
    return value or DEFAULT_TRACKING_URL


def make_state_file(query_id: str) -> Path:
    return (DEFAULT_STATE_DIR / f"{query_id}.txt").resolve()


def create_query() -> dict:
    query_id = uuid.uuid4().hex
    return {
        "id": query_id,
        "source_station": st.session_state["add_source"].strip(),
        "destination_station": st.session_state["add_destination"].strip(),
        "travel_date": st.session_state["add_travel_date"].strip(),
        "travel_class": st.session_state["add_travel_class"].strip(),
        "quota": st.session_state["add_quota"].strip() or "GN",
        "tracking_url": normalize_tracking_url(st.session_state["add_tracking_url"]),
        "train_number": st.session_state["add_train_number"].strip() or "NA",
        "pushover_user_key": st.session_state["add_pushover_user_key"].strip(),
        "pushover_app_token": st.session_state["add_pushover_app_token"].strip(),
        "pushover_device": st.session_state["add_pushover_device"].strip(),
        "state_file": str(make_state_file(query_id)),
        "timeout_seconds": int(st.session_state["add_timeout_seconds"]),
        "browser_wait_seconds": int(st.session_state["add_browser_wait_seconds"]),
        "use_browser_rendering": bool(st.session_state["add_use_browser_rendering"]),
        "cutoff": st.session_state["add_cutoff"].strip(),
        "poll_interval_seconds": 90,
    }


def build_query_config(query: dict) -> Config:
    cutoff_value = query.get("cutoff", "").strip()
    return Config(
        train_number=query.get("train_number", "NA") or "NA",
        source_station=query["source_station"],
        destination_station=query["destination_station"],
        travel_date=query["travel_date"],
        travel_class=query["travel_class"],
        quota=query.get("quota", "GN") or "GN",
        tracking_url=query["tracking_url"],
        pushover_user_key=query.get("pushover_user_key", ""),
        pushover_app_token=query.get("pushover_app_token", ""),
        pushover_device=query.get("pushover_device", ""),
        state_file=Path(query.get("state_file", make_state_file(query["id"]))),
        timeout_seconds=int(query.get("timeout_seconds", 20)),
        poll_interval_seconds=int(query.get("poll_interval_seconds", 90)),
        use_browser_rendering=bool(query.get("use_browser_rendering", True)),
        browser_wait_seconds=int(query.get("browser_wait_seconds", 7)),
        cutoff_at=datetime.fromisoformat(cutoff_value) if cutoff_value else None,
    )


def add_query() -> None:
    query = create_query()
    try:
        build_query_config(query)
    except ValueError as exc:
        st.error(f"Invalid cutoff: {exc}")
        return
    st.session_state["queries"].append(query)
    st.toast("Query added", icon="✅")


def remove_query(query_id: str) -> None:
    remaining = []
    for query in st.session_state["queries"]:
        if query["id"] == query_id:
            try:
                Path(query["state_file"]).unlink(missing_ok=True)
            except Exception:
                pass
            continue
        remaining.append(query)
    st.session_state["queries"] = remaining
    st.rerun()


def run_query_cycle(query: dict) -> tuple[list, bool, str]:
    config = build_query_config(query)
    session = requests.Session()
    entries = fetch_availability(session, config)
    signature = state_signature(config, entries)
    changed = False

    if alert_condition_met(entries):
        last_alert = read_last_alert(config.state_file)
        if last_alert != signature:
            send_pushover_alert(session, config, entries)
            write_last_alert(config.state_file, signature)
            changed = True
    else:
        clear_last_alert(config.state_file)

    return entries, changed, summarize_entries(entries)


def render_query_card(query: dict, auto_run: bool) -> None:
    config = build_query_config(query)
    with st.container(border=True):
        col_left, col_right = st.columns([4, 1])
        with col_left:
            st.markdown(
                f"**{query['source_station']} → {query['destination_station']}**  \n"
                f"{query['travel_date']} | {query['travel_class']} | {query['quota']}  \n"
                f"{query['tracking_url']}"
            )
        with col_right:
            if st.button("Remove", key=f"remove-{query['id']}"):
                remove_query(query["id"])
                return

        if is_cutoff_reached(config):
            st.caption("Cutoff reached; monitoring stopped for this query.")
        elif auto_run:
            try:
                entries, sent, summary = run_query_cycle(query)
                if entries:
                    actionable = [entry for entry in entries if entry.status in {"AVAILABLE", "CURR_AVBL"}]
                    st.success(f"{len(entries)} candidate options found")
                    for entry in entries:
                        label = f"{entry.train_name} {entry.train_number} - {entry.train_class} ({entry.status})"
                        st.write(f"🚨 {label}" if entry.status in {"AVAILABLE", "CURR_AVBL"} else label)
                    st.caption(f"Parsed availability: {len(actionable)} actionable, {len(entries) - len(actionable)} waitlist/other")
                else:
                    st.info("No current availability rows found.")

                if sent:
                    st.toast("Alert sent", icon="🔔")
                elif entries:
                    st.caption(f"Latest output: {summary}")
            except Exception as exc:
                st.error(f"Check failed: {exc}")
        else:
            st.caption("Monitoring paused for this query.")


init_state()

st.title("IRCTC Current Availability Monitor")
st.caption("Local UI with per-query refresh every 90 seconds.")

with st.sidebar:
    st.header("Add Query")
    st.session_state["add_source"] = st.text_input("Source station", value=st.session_state["add_source"], key="add_source_input")
    st.session_state["add_destination"] = st.text_input("Destination station", value=st.session_state["add_destination"], key="add_destination_input")
    st.session_state["add_travel_date"] = st.text_input("Travel date", value=st.session_state["add_travel_date"], key="add_travel_date_input")
    st.session_state["add_travel_class"] = st.selectbox("Travel class", ["SL", "3A", "2A", "1A", "CC", "EC"], index=["SL", "3A", "2A", "1A", "CC", "EC"].index(st.session_state["add_travel_class"]) if st.session_state["add_travel_class"] in ["SL", "3A", "2A", "1A", "CC", "EC"] else 0, key="add_travel_class_input")
    st.session_state["add_quota"] = st.text_input("Quota", value=st.session_state["add_quota"], key="add_quota_input")
    st.session_state["add_tracking_url"] = st.text_input("Tracking URL", value=st.session_state["add_tracking_url"], key="add_tracking_url_input", help="Use the ConfirmTkt home page or a route URL/template.")
    st.session_state["add_train_number"] = st.text_input("Train number (optional)", value=st.session_state["add_train_number"], key="add_train_number_input")
    st.session_state["add_pushover_user_key"] = st.text_input("Pushover user key", value=st.session_state["add_pushover_user_key"], key="add_pushover_user_key_input", type="password")
    st.session_state["add_pushover_app_token"] = st.text_input("Pushover app token", value=st.session_state["add_pushover_app_token"], key="add_pushover_app_token_input", type="password")
    st.session_state["add_pushover_device"] = st.text_input("Pushover device", value=st.session_state["add_pushover_device"], key="add_pushover_device_input")
    st.session_state["add_timeout_seconds"] = st.number_input("Timeout seconds", min_value=5, max_value=120, value=int(st.session_state["add_timeout_seconds"]), key="add_timeout_seconds_input")
    st.session_state["add_browser_wait_seconds"] = st.number_input("Browser wait seconds", min_value=1, max_value=30, value=int(st.session_state["add_browser_wait_seconds"]), key="add_browser_wait_seconds_input")
    st.session_state["add_use_browser_rendering"] = st.checkbox("Use browser rendering", value=bool(st.session_state["add_use_browser_rendering"]), key="add_use_browser_rendering_input")
    st.session_state["add_cutoff"] = st.text_input(
        "Cutoff (optional ISO datetime)",
        value=st.session_state["add_cutoff"],
        key="add_cutoff_input",
        help="Example: 2026-06-18T23:00:00",
    )

    if st.button("Add query"):
        add_query()

    st.divider()
    st.header("Monitoring")
    st.session_state["monitoring_enabled"] = st.toggle("Enable auto refresh", value=st.session_state["monitoring_enabled"], key="monitoring_enabled_toggle")
    if st.button("Run all now"):
        st.session_state["run_once_all"] = True


queries = st.session_state["queries"]
active_queries = [query for query in queries if not is_cutoff_reached(build_query_config(query))]
auto_run = st.session_state["monitoring_enabled"] and bool(active_queries)

if auto_run:
    st_autorefresh(interval=90_000, key="irctc-refresh")

if not queries:
    st.info("Discover trains, filter candidates, and monitor availability by adding a query in the sidebar.")
else:
    st.caption("Discover trains → filter by train and class → parse availability → alert on AVAILABLE or CURR_AVBL")
    for query in list(queries):
        render_query_card(query, auto_run or st.session_state.get("run_once_all", False))

if st.session_state.get("run_once_all", False):
    st.session_state["run_once_all"] = False
