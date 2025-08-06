import streamlit as st
import pandas as pd
import requests
from PyPDF2 import PdfReader
from datetime import datetime, timedelta
import re
from collections import defaultdict
from io import BytesIO

st.title("VT-9 Daily Airspace Assigner ✈️")

# ========== UTILITY FUNCTIONS ==========
def parse_time(tstr):
    try:
        return datetime.strptime(tstr, "%H%M").time()
    except:
        return None

def time_range(start_str, end_str):
    start = datetime.strptime(start_str, "%H%M")
    end = datetime.strptime(end_str, "%H%M")
    return [int((start + timedelta(minutes=i)).strftime("%H%M")) for i in range(int((end - start).total_seconds() / 60))]

def better_extract_event_name(tokens):
    for token in reversed(tokens):
        if re.match(r"^[A-Z]{2,}\d{3,4}[A-Z]?$", token):
            return token
        if token == "LEAD":
            idx = tokens.index("LEAD")
            return tokens[idx - 1] if idx > 0 else None
    return None

def has_time_conflict(t_range, used_ranges):
    return any(not t_range.isdisjoint(used) for used in used_ranges)

# ========== DATE SELECTION ==========
st.markdown("### Select Schedule Date")
def format_date(d):
    return d.strftime("%Y-%m-%d")

def get_sched_url(date_obj):
    return f"https://www.cnatra.navy.mil/scheds/TW1/SQ-VT-9/!{date_obj.strftime('%Y-%m-%d')}!VT-9!Frontpage.pdf"

def fetch_pdf(url):
    response = requests.get(url)
    if response.status_code == 200:
        return BytesIO(response.content)
    return None

selected_date = st.date_input("Choose date", datetime.today())
sched_url = get_sched_url(selected_date)

if st.button("📥 Load and Process Schedule"):
    st.info(f"Fetching schedule from: {sched_url}")
    pdf_data = fetch_pdf(sched_url)

    if not pdf_data:
        st.error("Failed to retrieve PDF from CNATRA site.")
    else:
        reader = PdfReader(pdf_data)
        lines = []
        for page in reader.pages:
            lines.extend(page.extract_text().splitlines())

        # Extract LEAD flights
        flight_lines = [line.strip() for line in lines if line.strip() != ""]
        lead_flights = []

        for line in flight_lines:
            tokens = line.split()
            if "LEAD" in tokens or any(re.match(r"^SLD\d{4}$", tok) for tok in tokens):
                try:
                    to_time = parse_time(tokens[2])
                    land_time = parse_time(tokens[3])
                    event = better_extract_event_name(tokens)
                    if not event or not to_time:
                        continue

                    event_index = tokens.index(event) if event in tokens else -1
                    instructor_tokens = tokens[4:event_index] if event_index > 4 else []
                    instructor = " ".join(instructor_tokens)

                    lead_flights.append({
                        "event": event,
                        "prefix": event[:3],
                        "takeoff": to_time.strftime("%H%M"),
                        "land": land_time.strftime("%H%M"),
                        "instructor": instructor
                    })
                except:
                    continue

        # Define logic parameters
        airspace_rules = {
            "FTX": {"slots": 2, "preferred": "MOA 2"},
            "BFM": {"slots": 2, "preferred": "MOA 2"},
            "FRM": {"slots": 1, "preferred": "Area 4"},
            "DIV": {"slots": 2, "preferred": "Area 4"},
            "NFR": {"slots": 1, "preferred": "Area 4"},
            "SLD": {"slots": 2, "preferred": "Area 4"},
            "TAC": {"slots": 2, "preferred": "Area 4"},
            "DTF": {"slots": 2, "preferred": "MOA 2"},
        }

        frequency_pool = [
            {"freq_pair": "17/80", "chattermark": "246.8"},
            {"freq_pair": "18/81", "chattermark": "333"},
            {"freq_pair": "19/82", "chattermark": "357"},
            {"freq_pair": "20/83", "chattermark": "246.9"},
            {"freq_pair": "21/84", "chattermark": "299.2"},
        ]

        # Build TR usage time blocks
        tr_blocks = []
        for line in flight_lines:
            tokens = line.split()
            if any("TR" in tok for tok in tokens):
                try:
                    to_time = parse_time(tokens[2])
                    land_time = parse_time(tokens[3])
                    if to_time and land_time:
                        tr_blocks.append(time_range(to_time.strftime("%H%M"), land_time.strftime("%H%M")))
                except:
                    continue
        tr_minutes = set(minute for block in tr_blocks for minute in block)

        # Track usage
        area4_usage = defaultdict(int)
        moa2_usage = defaultdict(int)
        used_times_by_freq = defaultdict(list)

        # Assign logic
        for flight in lead_flights:
            t_range_list = time_range(flight["takeoff"], flight["land"])
            t_range = set(t_range_list)

            # Assign frequency
            assigned = False
            for freq in frequency_pool:
                if not has_time_conflict(t_range, used_times_by_freq[freq["freq_pair"]]):
                    flight["freq_pair"] = freq["freq_pair"]
                    flight["chattermark"] = freq["chattermark"]
                    used_times_by_freq[freq["freq_pair"]].append(t_range)
                    assigned = True
                    break
            if not assigned:
                flight["freq_pair"] = "UNASSIGNED"
                flight["chattermark"] = "UNASSIGNED"

            # Assign airspace if needed
            prefix = flight["prefix"]
            assigned_area = None
            if prefix in airspace_rules:
                slots_needed = airspace_rules[prefix]["slots"]
                preferred = airspace_rules[prefix]["preferred"]

                def has_capacity(area_usage, t_range, needed, area_name):
                    for t in t_range:
                        penalty = 1 if area_name == "Area 4" and t in tr_minutes else 0
                        if area_usage[t] + needed + penalty > 4:
                            return False
                    return True

                if preferred == "Area 4" and has_capacity(area4_usage, t_range_list, slots_needed, "Area 4"):
                    for t in t_range_list:
                        area4_usage[t] += slots_needed
                    assigned_area = "Area 4"
                elif preferred == "MOA 2" and has_capacity(moa2_usage, t_range_list, slots_needed, "MOA 2"):
                    for t in t_range_list:
                        moa2_usage[t] += slots_needed
                    assigned_area = "MOA 2"
                elif preferred == "Area 4" and has_capacity(moa2_usage, t_range_list, slots_needed, "MOA 2"):
                    for t in t_range_list:
                        moa2_usage[t] += slots_needed
                    assigned_area = "MOA 2"
                elif preferred == "MOA 2" and has_capacity(area4_usage, t_range_list, slots_needed, "Area 4"):
                    for t in t_range_list:
                        area4_usage[t] += slots_needed
                    assigned_area = "Area 4"

            flight["assigned_area"] = assigned_area or ""

        # Final display
        df = pd.DataFrame(lead_flights)
        st.success("✅ Schedule processed successfully!")
        st.dataframe(df)

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("📥 Download CSV", csv, f"vt9_schedule_{selected_date}.csv", "text/csv")
