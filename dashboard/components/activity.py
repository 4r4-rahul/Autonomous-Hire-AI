"""
Activity Log component — virtualized log viewer with filtering and timeline grouping.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import streamlit as st

_ACTIVITY_LOG = Path("/Users/rahul/Workspace/Job_Finder/data/activity_log.jsonl")

_LEVEL_COLORS = {
    "error":    "#ef4444",
    "warning":  "#f59e0b",
    "decision": "#3b82f6",
    "info":     "#64748b",
}
_LEVEL_ICONS = {
    "error":    "🔴",
    "warning":  "⚠️",
    "decision": "🔵",
    "info":     "·",
}


def _classify_entry(entry: dict) -> str:
    """Classify an entry's level, handling 'no-level' entries (decisions)."""
    lvl = entry.get("level", "")
    if lvl in ("error", "warning", "info"):
        return lvl
    if entry.get("event") == "decision" or entry.get("action"):
        return "decision"
    return "info"


def _entry_message(entry: dict) -> str:
    msg = entry.get("event") or entry.get("message") or entry.get("action") or ""
    outcome = entry.get("outcome", "")
    if outcome:
        msg = f"{msg}  →  {outcome}"
    return msg


def _load_entries(n: int = 500) -> list[dict]:
    if not _ACTIVITY_LOG.exists():
        return []
    try:
        lines = _ACTIVITY_LOG.read_text().strip().splitlines()
        entries = []
        for line in reversed(lines[-n:]):
            try:
                e = json.loads(line)
                e["_level"] = _classify_entry(e)
                entries.append(e)
            except Exception:
                pass
        return entries
    except Exception:
        return []


def render(state: dict | None, approvals: list[dict]) -> None:
    st.markdown("""
    <h2 style="color:#f1f5f9;font-size:1.5rem;margin:0 0 20px 0">📜 Activity Log</h2>
    """, unsafe_allow_html=True)

    entries = _load_entries(500)

    if not entries:
        st.markdown("""
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;
        padding:48px 32px;text-align:center">
            <div style="font-size:3rem;margin-bottom:12px">📜</div>
            <div style="color:#94a3b8;font-size:1rem">No activity logged yet. Run the pipeline to generate events.</div>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Filters ──────────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([2, 2, 1])

    with f1:
        all_agents = sorted({e.get("agent", "?") for e in entries})
        agent_filter = st.multiselect(
            "Agent",
            options=all_agents,
            placeholder="All agents",
            key="act_agent",
            label_visibility="collapsed",
        )
    with f2:
        level_filter = st.multiselect(
            "Level",
            options=["info", "decision", "warning", "error"],
            placeholder="All levels",
            key="act_level",
            label_visibility="collapsed",
        )
    with f3:
        keyword_search = st.text_input(
            "Search",
            placeholder="Keyword...",
            key="act_keyword",
            label_visibility="collapsed",
        )

    # Apply filters
    filtered = entries
    if agent_filter:
        filtered = [e for e in filtered if e.get("agent") in agent_filter]
    if level_filter:
        filtered = [e for e in filtered if e.get("_level") in level_filter]
    if keyword_search:
        kw = keyword_search.lower()
        filtered = [e for e in filtered if kw in json.dumps(e).lower()]

    st.markdown(
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">',
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns([4, 1])
    with c1:
        st.markdown(
            f'<div style="color:#64748b;font-size:0.8rem">'
            f'Showing {len(filtered)} of {len(entries)} events (newest first)</div>',
            unsafe_allow_html=True,
        )
    with c2:
        # Export button
        export_data = json.dumps(
            [{k: v for k, v in e.items() if k != "_level"} for e in filtered],
            indent=2,
        )
        st.download_button(
            "⬇ Export JSON",
            data=export_data,
            file_name="activity_log_export.json",
            mime="application/json",
        )
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Group by hour ─────────────────────────────────────────────────────────────
    grouped: dict[str, list[dict]] = defaultdict(list)
    for e in filtered:
        ts = e.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            hour_key = dt.strftime("%b %d %Y  %H:00")
        except Exception:
            hour_key = "Unknown Time"
        grouped[hour_key].append(e)

    # Sort groups newest-first
    sorted_groups = sorted(grouped.keys(), reverse=True)

    for hour_key in sorted_groups:
        group_entries = grouped[hour_key]

        # Hour header
        st.markdown(
            f'<div style="font-size:0.7rem;font-weight:600;color:#475569;'
            f'letter-spacing:0.08em;text-transform:uppercase;'
            f'padding:8px 0 4px 0;border-bottom:1px solid #1e293b;margin-bottom:6px">'
            f'{hour_key}  <span style="color:#334155;font-weight:400">({len(group_entries)} events)</span></div>',
            unsafe_allow_html=True,
        )

        for entry in group_entries:
            level   = entry.get("_level", "info")
            agent   = entry.get("agent", "?")
            msg     = _entry_message(entry)
            ts_str  = entry.get("timestamp", "")[:19]
            details = entry.get("details", {})
            color   = _LEVEL_COLORS.get(level, "#64748b")
            icon    = _LEVEL_ICONS.get(level, "·")

            # Inline entry row
            st.markdown(
                f'<div style="display:flex;align-items:baseline;gap:8px;'
                f'padding:4px 6px;border-radius:4px;margin-bottom:2px;'
                f'background:{"#ef444408" if level == "error" else ("rgba(245,158,11,0.05)" if level == "warning" else "transparent")}">'
                f'<span style="font-size:0.7rem;color:#334155;white-space:nowrap;min-width:50px">'
                f'{ts_str[11:] if ts_str else "?"}</span>'
                f'<span style="font-size:0.75rem;color:#3b82f6;min-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                f'{agent}</span>'
                f'<span style="font-size:0.8rem;color:{color}">{icon}</span>'
                f'<span style="font-size:0.8rem;color:#94a3b8;flex:1">{msg}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Expandable details for decisions and errors
            if (level in ("decision", "error")) and details:
                with st.expander(f"  details ({agent}/{entry.get('action', msg[:20])})", expanded=False):
                    st.json(details)
