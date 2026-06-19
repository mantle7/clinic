import os
from datetime import date, datetime, time, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st


DOCTOR_NAME = "Dr. Niyamath"


def get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    try:
        database_url = st.secrets.get("DATABASE_URL")
        if database_url:
            return database_url

        database_config = st.secrets.get("database", {})
        if database_config.get("url"):
            return database_config["url"]
    except Exception:
        pass

    raise RuntimeError(
        "Postgres is not configured. Add DATABASE_URL to Streamlit secrets or environment variables."
    )


def get_connection():
    return psycopg2.connect(get_database_url(), cursor_factory=RealDictCursor)


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS appointment_days (
                    id SERIAL PRIMARY KEY,
                    date TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CANCELLED', 'CLOSED'))
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS slots (
                    id SERIAL PRIMARY KEY,
                    day_id INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('AVAILABLE', 'BOOKED', 'BLOCKED')),
                    FOREIGN KEY(day_id) REFERENCES appointment_days(id),
                    UNIQUE(day_id, start_time)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS patients (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    age INTEGER,
                    gender TEXT,
                    UNIQUE(phone)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id SERIAL PRIMARY KEY,
                    patient_id INTEGER NOT NULL,
                    slot_id INTEGER NOT NULL UNIQUE,
                    reason TEXT,
                    status TEXT NOT NULL CHECK(status IN ('BOOKED', 'COMPLETED', 'CANCELLED', 'RESCHEDULED')),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(patient_id) REFERENCES patients(id),
                    FOREIGN KEY(slot_id) REFERENCES slots(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS waitlist (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    notified BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )


def generate_slot_times(start_value, end_value, duration_minutes):
    current = datetime.combine(date.today(), start_value)
    end = datetime.combine(date.today(), end_value)
    slot_times = []

    while current < end:
        slot_times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=duration_minutes)

    return slot_times


def display_date(date_text):
    return datetime.strptime(date_text, "%Y-%m-%d").strftime("%d %b %Y")


def display_time(time_text):
    return datetime.strptime(time_text, "%H:%M").strftime("%I:%M %p").lstrip("0")


def open_day(day_date, slot_times):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO appointment_days(date, status)
                VALUES (%s, 'OPEN')
                ON CONFLICT(date) DO UPDATE SET status = 'OPEN'
                RETURNING id
                """,
                (day_date.isoformat(),),
            )
            day_id = cursor.fetchone()["id"]

            for slot_time in slot_times:
                cursor.execute(
                    """
                    INSERT INTO slots(day_id, start_time, status)
                    VALUES (%s, %s, 'AVAILABLE')
                    ON CONFLICT(day_id, start_time) DO NOTHING
                    """,
                    (day_id, slot_time),
                )


def fetch_all(query, params=None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())
            return cursor.fetchall()


def fetch_one(query, params=None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())
            return cursor.fetchone()


def execute_query(query, params=None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())


def get_open_days():
    return fetch_all(
        """
        SELECT d.id, d.date, COUNT(s.id) AS available_slots
        FROM appointment_days d
        JOIN slots s ON s.day_id = d.id
        WHERE d.status = 'OPEN' AND s.status = 'AVAILABLE'
        GROUP BY d.id, d.date
        ORDER BY d.date
        """
    )


def get_available_slots(day_id):
    return fetch_all(
        """
        SELECT id, start_time
        FROM slots
        WHERE day_id = %s AND status = 'AVAILABLE'
        ORDER BY start_time
        """,
        (day_id,),
    )


def book_appointment(slot_id, name, phone, age, gender, reason):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM slots WHERE id = %s FOR UPDATE", (slot_id,))
            slot = cursor.fetchone()

            if not slot or slot["status"] != "AVAILABLE":
                return False, "This slot is no longer available."

            cursor.execute(
                """
                INSERT INTO patients(name, phone, age, gender)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(phone) DO UPDATE SET
                    name = excluded.name,
                    age = excluded.age,
                    gender = excluded.gender
                RETURNING id
                """,
                (name, phone, age, gender),
            )
            patient_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                INSERT INTO appointments(patient_id, slot_id, reason, status, created_at)
                VALUES (%s, %s, %s, 'BOOKED', %s)
                """,
                (patient_id, slot_id, reason, datetime.now().isoformat(timespec="seconds")),
            )
            cursor.execute("UPDATE slots SET status = 'BOOKED' WHERE id = %s", (slot_id,))
            return True, "Appointment confirmed."


def get_upcoming_bookings():
    return fetch_all(
        """
        SELECT
            a.id,
            d.date,
            s.start_time,
            p.name,
            p.phone,
            p.age,
            p.gender,
            a.reason,
            a.status
        FROM appointments a
        JOIN patients p ON p.id = a.patient_id
        JOIN slots s ON s.id = a.slot_id
        JOIN appointment_days d ON d.id = s.day_id
        WHERE a.status = 'BOOKED' AND d.status = 'OPEN'
        ORDER BY d.date, s.start_time
        """
    )


def get_all_days():
    return fetch_all(
        """
        SELECT
            d.id,
            d.date,
            d.status,
            COUNT(s.id) AS total_slots,
            SUM(CASE WHEN s.status = 'BOOKED' THEN 1 ELSE 0 END) AS booked_slots
        FROM appointment_days d
        LEFT JOIN slots s ON s.day_id = d.id
        GROUP BY d.id, d.date, d.status
        ORDER BY d.date DESC
        """
    )


def cancel_day(day_id):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.name, p.phone, d.date, s.start_time
                FROM appointments a
                JOIN patients p ON p.id = a.patient_id
                JOIN slots s ON s.id = a.slot_id
                JOIN appointment_days d ON d.id = s.day_id
                WHERE d.id = %s AND a.status = 'BOOKED'
                ORDER BY s.start_time
                """,
                (day_id,),
            )
            bookings = cursor.fetchall()

            cursor.execute("UPDATE appointment_days SET status = 'CANCELLED' WHERE id = %s", (day_id,))
            cursor.execute(
                """
                UPDATE appointments
                SET status = 'CANCELLED'
                WHERE slot_id IN (SELECT id FROM slots WHERE day_id = %s)
                """,
                (day_id,),
            )
            cursor.execute("UPDATE slots SET status = 'BLOCKED' WHERE day_id = %s", (day_id,))
            return bookings


def add_to_waitlist(name, phone):
    execute_query(
        """
        INSERT INTO waitlist(name, phone, created_at)
        VALUES (%s, %s, %s)
        """,
        (name, phone, datetime.now().isoformat(timespec="seconds")),
    )


def get_waitlist():
    return fetch_all(
        """
        SELECT id, name, phone, created_at, notified
        FROM waitlist
        ORDER BY created_at DESC
        """
    )


def mark_waitlist_notified():
    execute_query("UPDATE waitlist SET notified = TRUE WHERE notified = FALSE")


def customer_view():
    st.header("Book Appointment")

    open_days = get_open_days()
    if not open_days:
        st.info("No appointment days are open right now.")
        with st.form("waitlist_form"):
            st.subheader("Notify Me")
            name = st.text_input("Name")
            phone = st.text_input("Phone Number", placeholder="+91 XXXXX XXXXX")
            submitted = st.form_submit_button("Join Waitlist")

        if submitted:
            if not name.strip() or not phone.strip():
                st.error("Please enter name and phone number.")
            else:
                add_to_waitlist(name.strip(), phone.strip())
                st.success("You have been added to the waitlist.")
        return

    day_options = {
        f"{display_date(row['date'])} ({row['available_slots']} slots)": row["id"]
        for row in open_days
    }
    selected_day_label = st.selectbox("Available OPD Days", list(day_options.keys()))
    selected_day_id = day_options[selected_day_label]

    slots = get_available_slots(selected_day_id)
    slot_options = {display_time(row["start_time"]): row["id"] for row in slots}
    selected_slot_label = st.radio("Available Slots", list(slot_options.keys()), horizontal=True)

    with st.form("booking_form"):
        st.subheader("Patient Details")
        phone = st.text_input("Phone Number", placeholder="+91 XXXXX XXXXX")
        name = st.text_input("Name")
        age = st.number_input("Age", min_value=0, max_value=120, step=1)
        gender = st.selectbox("Gender", ["Female", "Male", "Other", "Prefer not to say"])
        reason = st.text_area("Reason (optional)")
        submitted = st.form_submit_button("Book")

    if submitted:
        if not phone.strip() or not name.strip():
            st.error("Please enter phone number and name.")
            return

        success, message = book_appointment(
            slot_options[selected_slot_label],
            name.strip(),
            phone.strip(),
            age,
            gender,
            reason.strip(),
        )

        if success:
            st.success(message)
            st.write("Date:", selected_day_label.split(" (")[0])
            st.write("Time:", selected_slot_label)
            st.write("Doctor:", DOCTOR_NAME)
            st.rerun()
        else:
            st.error(message)


def admin_view():
    st.header("Admin Dashboard")

    with st.expander("Open Appointment Day", expanded=True):
        with st.form("open_day_form"):
            col1, col2 = st.columns(2)
            with col1:
                day_date = st.date_input("Date", min_value=date.today())
                start_time = st.time_input("Start Time", value=time(9, 0))
            with col2:
                end_time = st.time_input("End Time", value=time(12, 0))
                duration = st.number_input("Slot Duration (minutes)", min_value=5, max_value=120, value=15, step=5)

            submitted = st.form_submit_button("Generate & Publish Slots")

        if submitted:
            if end_time <= start_time:
                st.error("End time must be after start time.")
            else:
                slot_times = generate_slot_times(start_time, end_time, duration)
                open_day(day_date, slot_times)
                st.success(f"Published {len(slot_times)} slots for {day_date.strftime('%d %b %Y')}.")

    st.subheader("Upcoming Appointments")
    bookings = get_upcoming_bookings()
    if bookings:
        st.dataframe(
            [
                {
                    "Date": display_date(row["date"]),
                    "Time": display_time(row["start_time"]),
                    "Patient": row["name"],
                    "Phone": row["phone"],
                    "Age": row["age"],
                    "Gender": row["gender"],
                    "Reason": row["reason"],
                }
                for row in bookings
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No upcoming bookings.")

    st.subheader("Cancel Day")
    days = get_all_days()
    cancellable_days = [row for row in days if row["status"] == "OPEN"]

    if cancellable_days:
        day_options = {
            f"{display_date(row['date'])} - {row['booked_slots'] or 0}/{row['total_slots']} booked": row["id"]
            for row in cancellable_days
        }
        selected_day = st.selectbox("Select day to cancel", list(day_options.keys()))

        if st.button("Cancel Entire Day", type="primary"):
            cancelled_bookings = cancel_day(day_options[selected_day])
            st.warning(f"Cancelled {len(cancelled_bookings)} appointments for {selected_day.split(' - ')[0]}.")
            if cancelled_bookings:
                st.write("Messages to send:")
                for row in cancelled_bookings:
                    st.code(
                        f"Dear {row['name']}, your appointment on "
                        f"{display_date(row['date'])} at {display_time(row['start_time'])} "
                        "has been cancelled. Please rebook once new slots become available."
                    )
    else:
        st.info("No open days to cancel.")

    st.subheader("Appointment Days")
    if days:
        st.dataframe(
            [
                {
                    "Date": display_date(row["date"]),
                    "Status": row["status"],
                    "Total Slots": row["total_slots"],
                    "Booked": row["booked_slots"] or 0,
                }
                for row in days
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Waitlist")
    waitlist = get_waitlist()
    if waitlist:
        st.dataframe(
            [
                {
                    "Name": row["name"],
                    "Phone": row["phone"],
                    "Created": row["created_at"],
                    "Notified": "Yes" if row["notified"] else "No",
                }
                for row in waitlist
            ],
            use_container_width=True,
            hide_index=True,
        )
        if st.button("Mark Waitlist as Notified"):
            mark_waitlist_notified()
            st.success("Waitlist marked as notified.")
            st.rerun()
    else:
        st.info("No waitlist entries.")


def main():
    st.set_page_config(page_title="Clinic Appointments", page_icon=":hospital:", layout="wide")
    st.title("Clinic Appointments")

    try:
        init_db()
    except Exception:
        st.error("Postgres connection is not configured or is unavailable.")
        st.info("Set DATABASE_URL in Streamlit Cloud secrets, then restart the app.")
        st.code('DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE"')
        st.stop()

    if "show_admin_login" not in st.session_state:
        st.session_state.show_admin_login = False
    if "admin_authenticated" not in st.session_state:
        st.session_state.admin_authenticated = False

    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("Admin View"):
            st.session_state.show_admin_login = True

    if st.session_state.admin_authenticated:
        if st.button("Customer View"):
            st.session_state.admin_authenticated = False
            st.session_state.show_admin_login = False
            st.rerun()
        admin_view()
        return

    if st.session_state.show_admin_login:
        with st.form("admin_login_form"):
            st.subheader("Admin Login")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")

        if submitted:
            if username == "admin" and password == "admin":
                st.session_state.admin_authenticated = True
                st.success("Logged in as admin.")
                st.rerun()
            else:
                st.error("Invalid username or password.")

    customer_view()


if __name__ == "__main__":
    main()
