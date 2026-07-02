import csv
import io
from datetime import datetime
import pandas as pd
from app.extensions import db
from app.models import Profile, User

PROFILE_EXPORT_COLUMNS = [
    "profile_code", "full_name", "gender", "candidate_type", "date_of_birth",
    "height_cm", "weight_kg", "blood_group", "sindhi_caste", "sub_caste",
    "marital_status", "occupation", "qualification", "current_city",
    "current_state", "phone", "email", "approval_status", "created_at",
]


def export_profiles_csv(profiles):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(PROFILE_EXPORT_COLUMNS)
    for p in profiles:
        writer.writerow([getattr(p, col) for col in PROFILE_EXPORT_COLUMNS])
    output.seek(0)
    return output.getvalue()


def export_profiles_excel(profiles):
    rows = []
    for p in profiles:
        rows.append({col: getattr(p, col) for col in PROFILE_EXPORT_COLUMNS})
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Profiles")
    buf.seek(0)
    return buf


REQUIRED_IMPORT_COLUMNS = {"full_name", "gender", "candidate_type", "email", "phone", "date_of_birth"}
ALLOWED_CANDIDATE_TYPES = {"groom", "bride"}
ALLOWED_GENDERS = {"male", "female"}
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y")
MAX_IMPORT_ROWS = 1000


def _parse_dob(raw_value):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        raise ValueError("date_of_birth is empty")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw_value, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"could not parse date_of_birth '{raw_value}' "
        f"(accepted formats: YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, MM/DD/YYYY, DD.MM.YYYY)"
    )


def import_profiles_csv(file_stream, created_by_user_id):
    """
    Bulk import candidate profiles from a CSV file-like object.

    Performance notes (this matters a lot for large files on a single
    web worker): we do NOT hash a fresh bcrypt password per row (bcrypt is
    intentionally slow, ~200-300ms per call — at 12 rounds, a few hundred
    rows could easily blow past a request timeout on its own). Instead we
    hash the shared temporary password once and reuse that hash for every
    imported row (all imported accounts share the same temp password and
    are forced to change it on first login anyway). We also pre-fetch all
    existing emails/phones in two queries instead of two-per-row, validate
    everything in plain Python first, and perform a single database commit
    at the end instead of one commit per row (each commit forces a disk
    sync on SQLite, which is slow to do hundreds of times in a row).

    Returns (success_count, errors[list of strings]).
    """
    text = file_stream.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    elif isinstance(text, str):
        text = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames or not REQUIRED_IMPORT_COLUMNS.issubset(
        {(f or "").strip() for f in reader.fieldnames}
    ):
        missing = REQUIRED_IMPORT_COLUMNS - {(f or "").strip() for f in (reader.fieldnames or [])}
        return 0, [f"Missing required columns: {', '.join(sorted(missing))}"]

    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        return 0, [f"CSV has {len(rows)} rows, which exceeds the {MAX_IMPORT_ROWS}-row import limit. "
                   f"Please split the file into smaller batches."]
    if not rows:
        return 0, ["CSV file has no data rows."]

    # Hash the shared temporary password ONCE, not once per row.
    shared_password_hash = User(full_name="_", email="_", role="groom")
    shared_password_hash.set_password("Welcome@123")
    shared_password_hash = shared_password_hash.password_hash

    # Pre-fetch existing emails/phones once instead of 2 queries per row.
    existing_emails = {e for (e,) in db.session.query(User.email).all()}
    existing_phones = {p for (p,) in db.session.query(User.phone).all() if p}

    seen_emails_in_batch = set()
    seen_phones_in_batch = set()

    valid_rows = []  # list of dicts ready to build User/Profile objects
    errors = []

    for i, row in enumerate(rows, start=2):
        try:
            full_name = (row.get("full_name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            phone = (row.get("phone") or "").strip()
            gender = (row.get("gender") or "").strip().lower()
            candidate_type = (row.get("candidate_type") or "").strip().lower()

            if not full_name:
                raise ValueError("full_name is empty")
            if not email:
                raise ValueError("email is empty")
            if not phone:
                raise ValueError("phone is empty")
            if gender not in ALLOWED_GENDERS:
                raise ValueError(f"gender must be 'male' or 'female', got '{gender}'")
            if candidate_type not in ALLOWED_CANDIDATE_TYPES:
                raise ValueError(f"candidate_type must be 'groom' or 'bride', got '{candidate_type}'")

            dob = _parse_dob(row.get("date_of_birth"))

            if email in existing_emails:
                raise ValueError(f"email {email} already exists, skipped")
            if phone in existing_phones:
                raise ValueError(f"phone {phone} already exists, skipped")
            if email in seen_emails_in_batch:
                raise ValueError(f"email {email} is duplicated elsewhere in this CSV, skipped")
            if phone in seen_phones_in_batch:
                raise ValueError(f"phone {phone} is duplicated elsewhere in this CSV, skipped")

            seen_emails_in_batch.add(email)
            seen_phones_in_batch.add(phone)

            valid_rows.append({
                "full_name": full_name, "email": email, "phone": phone,
                "gender": gender, "candidate_type": candidate_type, "dob": dob,
                "current_city": (row.get("current_city") or "").strip() or None,
                "occupation": (row.get("occupation") or "").strip() or None,
                "qualification": (row.get("qualification") or "").strip() or None,
                "sindhi_caste": (row.get("sindhi_caste") or "").strip() or None,
            })
        except Exception as e:  # noqa: BLE001
            errors.append(f"Row {i}: {str(e)}")

    if not valid_rows:
        return 0, errors

    try:
        created_profiles = []
        for data in valid_rows:
            user = User(
                full_name=data["full_name"], email=data["email"], phone=data["phone"],
                role=data["candidate_type"], status="active",
                created_by_id=created_by_user_id, must_change_password=True,
            )
            user.password_hash = shared_password_hash  # reuse pre-computed hash
            db.session.add(user)
            db.session.flush()

            profile = Profile(
                user_id=user.id, full_name=data["full_name"], gender=data["gender"],
                candidate_type=data["candidate_type"], date_of_birth=data["dob"],
                current_city=data["current_city"], occupation=data["occupation"],
                qualification=data["qualification"], phone=data["phone"], email=data["email"],
                sindhi_caste=data["sindhi_caste"], approval_status="pending",
            )
            db.session.add(profile)
            created_profiles.append(profile)

        db.session.flush()  # assign profile.id values
        for profile in created_profiles:
            profile.profile_code = _generate_profile_code(profile)

        db.session.commit()
        return len(created_profiles), errors
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        errors.append(f"Batch insert failed, no rows were imported: {str(e)}")
        return 0, errors


def _generate_profile_code(profile):
    prefix = "B" if profile.candidate_type == "bride" else "G"
    return f"CSSS-{prefix}-{profile.id:04d}"
