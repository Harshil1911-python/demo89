"""
Handles admin/member-assisted profile creation for candidates who register
in person at the bureau office rather than self-registering online.
"""
import secrets
from datetime import datetime
from app.extensions import db
from app.models import User, Profile, Photo, Document
from app.utils.helpers import generate_profile_code
from app.services.upload_service import save_photo, save_document, save_kundli
from app.services.qr_service import generate_profile_qr

DOC_FIELD_MAP = {
    "aadhar_doc": "aadhar",
    "passport_doc": "passport",
    "pan_doc": "pan",
    "driving_license_doc": "driving_license",
    "other_doc": "other",
}


def create_walkin_profile(form, created_by_user_id, auto_approve=False):
    """
    Creates a new User + Profile from a ProfileForm submitted by staff
    (admin or member) on behalf of a walk-in candidate.

    Returns (profile_or_None, temp_password_or_None, errors_list).
    """
    email = (form.email.data or "").strip().lower()
    phone = (form.phone.data or "").strip()

    if not email:
        return None, None, ["An email address is required to create a login for this candidate."]
    if User.query.filter_by(email=email).first():
        return None, None, [f"A user with email {email} already exists."]
    if phone and User.query.filter_by(phone=phone).first():
        return None, None, [f"A user with phone {phone} already exists."]

    temp_password = secrets.token_urlsafe(6)

    try:
        user = User(
            full_name=form.full_name.data.strip(),
            email=email,
            phone=phone or None,
            role=form.candidate_type.data,
            status="active",
            created_by_id=created_by_user_id,
            must_change_password=True,
        )
        user.set_password(temp_password)
        db.session.add(user)
        db.session.flush()

        profile = Profile(user_id=user.id)
        form.populate_obj(profile)
        profile.kundli_available = form.kundli_available.data
        db.session.add(profile)
        db.session.flush()
        profile.profile_code = generate_profile_code(profile)

        # Photos
        uploaded_files = [f for f in (form.photo_files.data or []) if f and getattr(f, "filename", "")]
        for f in uploaded_files:
            rel, thumb = save_photo(f)
            if rel:
                db.session.add(Photo(profile_id=profile.id, file_path=rel, thumbnail_path=thumb))
        db.session.flush()
        first_photo = Photo.query.filter_by(profile_id=profile.id).order_by(Photo.id).first()
        if first_photo:
            profile.featured_photo_path = first_photo.file_path
            first_photo.is_featured = True

        # Documents
        for field_name, doc_type in DOC_FIELD_MAP.items():
            file_storage = getattr(form, field_name).data
            if file_storage and getattr(file_storage, "filename", ""):
                rel, original = save_document(file_storage)
                if rel:
                    db.session.add(Document(profile_id=profile.id, doc_type=doc_type,
                                             file_path=rel, original_filename=original))

        if form.kundli_file.data and getattr(form.kundli_file.data, "filename", ""):
            kundli_rel = save_kundli(form.kundli_file.data)
            if kundli_rel:
                db.session.add(Document(profile_id=profile.id, doc_type="kundli", file_path=kundli_rel))
                profile.kundli_available = True

        if auto_approve:
            profile.approval_status = "approved"
            profile.is_verified = True
            profile.approved_by_id = created_by_user_id
            profile.approved_at = datetime.utcnow()
        else:
            profile.approval_status = "pending"

        db.session.commit()

        if auto_approve:
            profile.qr_path = generate_profile_qr(profile)
            db.session.commit()

        return profile, temp_password, []
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return None, None, [f"Could not create profile: {str(e)}"]
