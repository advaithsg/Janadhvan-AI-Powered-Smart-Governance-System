"""Smart Emergency Chain — dispatches alerts to response agencies by category."""
from datetime import datetime
        
AGENCIES = {       
    "hospital": [
        {"name": "City General Hospital", "phone": "108", "unit": "Ambulance & Trauma"},
        {"name": "District Medical Center", "phone": "+91-80-2222-3333", "unit": "Emergency Ward"},
        {"name": "Apollo Civic Hospital", "phone": "1066", "unit": "24/7 Emergency"},
    ],
    "fire": [
        {"name": "Municipal Fire & Rescue", "phone": "101", "unit": "Fire Brigade HQ"},
        {"name": "Industrial Safety Fire Unit", "phone": "+91-80-4444-5555", "unit": "Hazmat Response"},
    ],
    "disaster": [
        {"name": "State Disaster Response Force (SDRF)", "phone": "1070", "unit": "Flood & Collapse"},
        {"name": "National Disaster Response", "phone": "011-26701728", "unit": "NDRF Coordination"},
        {"name": "Civic Disaster Control Room", "phone": "1077", "unit": "Municipal Emergency"},
    ],
}

CATEGORY_AGENCIES = {
    "Fire Emergency": ["fire", "disaster"],
    "Medical Emergency": ["hospital", "disaster"],
    "Flood": ["disaster", "fire", "hospital"],
    "Gas Leakage": ["fire", "disaster"],
    "Accident": ["hospital", "fire", "disaster"],
}


def agencies_for_category(category):
    types = CATEGORY_AGENCIES.get(category, ["hospital", "fire", "disaster"])
    result = []
    for agency_type in types:
        for agency in AGENCIES[agency_type]:
            result.append({**agency, "agency_type": agency_type})
    return result


def dispatch_emergency_chain(db, complaint_id, category, latitude=None, longitude=None):
    """Record emergency chain dispatches for a critical complaint."""
    now = datetime.utcnow().isoformat()
    loc_note = ""
    if latitude and longitude:
        loc_note = f" GPS: {latitude:.5f}, {longitude:.5f}"

    dispatched = []
    for agency in agencies_for_category(category):
        message = (
            f"CRITICAL EMERGENCY #{complaint_id} ({category}).{loc_note} "
            f"Immediate response required at reported location."
        )
        db.execute(
            """
            INSERT INTO emergency_dispatches (
                complaint_id, agency_type, agency_name, contact_phone,
                unit, message, status, dispatched_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'Dispatched', ?)
            """,
            (
                complaint_id,
                agency["agency_type"],
                agency["name"],
                agency["phone"],
                agency["unit"],
                message,
                now,
            ),
        )
        dispatched.append(agency)
    db.commit()
    return dispatched


def get_dispatches_for_complaint(db, complaint_id):
    return db.execute(
        """
        SELECT * FROM emergency_dispatches WHERE complaint_id = ?
        ORDER BY agency_type, dispatched_at DESC
        """,
        (complaint_id,),
    ).fetchall()


def get_recent_dispatches(db, limit=20):
    return db.execute(
        """
        SELECT e.*, c.title, c.category, c.area
        FROM emergency_dispatches e
        JOIN complaints c ON c.id = e.complaint_id
        ORDER BY e.dispatched_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
