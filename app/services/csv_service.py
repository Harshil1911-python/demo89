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
    Each row is processed in its own savepoint, so a bad row is skipped
    without corrupting the rest of the import batch.
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

    success = 0
    errors = []

    for i, row in enumerate(reader, start=2):
        # Each row gets its own SAVEPOINT so a failure here can be rolled back
        # in isolation without poisoning the rest of the batch.
        savepoint = db.session.begin_nested()
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

            if User.query.filter_by(email=email).first():
                raise ValueError(f"email {email} already exists, skipped")
            if User.query.filter_by(phone=phone).first():
                raise ValueError(f"phone {phone} already exists, skipped")

            user = User(
                full_name=full_name,
                email=email,
                phone=phone,
                role=candidate_type,
                status="active",
                created_by_id=created_by_user_id,
                must_change_password=True,
            )
            user.set_password("Welcome@123")
            db.session.add(user)
            db.session.flush()

            profile = Profile(
                user_id=user.id,
                full_name=full_name,
                gender=gender,
                candidate_type=candidate_type,
                date_of_birth=dob,
                current_city=(row.get("current_city") or "").strip() or None,
                occupation=(row.get("occupation") or "").strip() or None,
                qualification=(row.get("qualification") or "").strip() or None,
                phone=phone,
                email=email,
                sindhi_caste=(row.get("sindhi_caste") or "").strip() or None,
                approval_status="pending",
            )
            db.session.add(profile)
            db.session.flush()
            profile.profile_code = _generate_profile_code(profile)

            savepoint.commit()
            success += 1
        except Exception as e:  # noqa: BLE001
            savepoint.rollback()
            errors.append(f"Row {i}: {str(e)}")

    db.session.commit()
    return success, errors


def _generate_profile_code(profile):
    prefix = "B" if profile.candidate_type == "bride" else "G"
    return f"CSSS-{prefix}-{profile.id:04d}"
