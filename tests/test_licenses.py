from datetime import UTC, datetime, timedelta

from app.core.keys import generate_key, hash_key
from app.models.models import License, LicenseCheckIn


def _make_license(db, **overrides):
    raw_key = generate_key()
    defaults = dict(
        key_hash=hash_key(raw_key),
        key_last4=raw_key[-4:],
        tier="self_host_standard",
        monthly_run_cap=500,
        status="active",
        issued_at=datetime.now(tz=UTC).replace(tzinfo=None),
        renews_at=datetime.now(tz=UTC).replace(tzinfo=None) + timedelta(days=365),
        issued_by="test",
    )
    defaults.update(overrides)
    license_row = License(**defaults)
    db.add(license_row)
    db.commit()
    db.refresh(license_row)
    return raw_key, license_row


def _check_in(client, raw_key, install_id="install-1"):
    return client.post(
        "/v1/licenses/check-in",
        json={"install_id": install_id, "product": "sentinel_ai"},
        headers={"Authorization": f"Bearer {raw_key}"},
    )


# ── Missing/malformed auth ──────────────────────────────────────────


def test_missing_authorization_header_is_401(client):
    r = client.post("/v1/licenses/check-in", json={"install_id": "x"})
    assert r.status_code == 401


def test_malformed_authorization_header_is_401(client):
    r = client.post(
        "/v1/licenses/check-in", json={"install_id": "x"},
        headers={"Authorization": "NotBearer something"},
    )
    assert r.status_code == 401


def test_empty_bearer_token_is_401(client):
    r = client.post(
        "/v1/licenses/check-in", json={"install_id": "x"},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 401


# ── The always-200 contract ─────────────────────────────────────────


def test_unknown_key_returns_200_valid_false_not_found(client):
    r = _check_in(client, "slk_" + "0" * 32)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "not_found"


def test_active_license_returns_200_valid_true_with_entitlements(client, db_session):
    raw_key, license_row = _make_license(db_session, tier="self_host_standard", monthly_run_cap=500)
    r = _check_in(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["reason"] is None
    assert body["tier"] == "self_host_standard"
    assert body["status"] == "active"
    assert body["monthly_run_cap"] == 500
    assert body["renews_at"] is not None


def test_revoked_license_returns_200_valid_false(client, db_session):
    raw_key, _ = _make_license(db_session, status="revoked")
    r = _check_in(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "revoked"


def test_suspended_license_returns_200_valid_false(client, db_session):
    raw_key, _ = _make_license(db_session, status="suspended")
    r = _check_in(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "suspended"


def test_expired_license_returns_200_valid_false(client, db_session):
    raw_key, _ = _make_license(
        db_session,
        status="active",
        renews_at=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=1),
    )
    r = _check_in(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "expired"


def test_unrecognized_status_fails_closed(client, db_session):
    # Regression: status classification must be an allow-list (only
    # "active" passes) so a typo, data corruption, or a future status
    # value nothing here recognizes yet is denied by construction,
    # never silently treated as valid.
    raw_key, _ = _make_license(db_session, status="trial_expired")
    r = _check_in(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "trial_expired"


def test_perpetual_license_never_expires(client, db_session):
    raw_key, _ = _make_license(db_session, renews_at=None)
    r = _check_in(client, raw_key)
    body = r.json()
    assert body["valid"] is True
    assert body["renews_at"] is None


# ── Side effects: last-seen + check-in log ──────────────────────────


def test_checkin_updates_last_seen(client, db_session):
    raw_key, license_row = _make_license(db_session)
    assert license_row.last_seen_at is None

    _check_in(client, raw_key, install_id="install-abc")

    db_session.refresh(license_row)
    assert license_row.last_seen_at is not None
    assert license_row.last_seen_ip is not None


def test_checkin_is_logged_for_valid_key(client, db_session):
    raw_key, license_row = _make_license(db_session)
    _check_in(client, raw_key, install_id="install-abc")

    rows = db_session.query(LicenseCheckIn).filter_by(license_id=license_row.id).all()
    assert len(rows) == 1
    assert rows[0].result == "ok"
    assert rows[0].install_id == "install-abc"


def test_checkin_is_logged_for_not_found_key(client, db_session):
    _check_in(client, "slk_" + "1" * 32)

    rows = db_session.query(LicenseCheckIn).filter_by(license_id=None).all()
    assert len(rows) == 1
    assert rows[0].result == "not_found"


def test_revoked_key_still_updates_last_seen(client, db_session):
    # A revoked key checking in is still a useful signal (an install
    # that hasn't noticed the revocation yet) — last_seen tracking
    # should not stop just because access was denied.
    raw_key, license_row = _make_license(db_session, status="revoked")
    assert license_row.last_seen_at is None

    _check_in(client, raw_key)

    db_session.refresh(license_row)
    assert license_row.last_seen_at is not None


# ── Rate limiting ────────────────────────────────────────────────────


def test_check_in_is_rate_limited_per_ip(client, db_session):
    raw_key, _ = _make_license(db_session)

    responses = [_check_in(client, raw_key) for _ in range(21)]

    assert all(r.status_code == 200 for r in responses[:20])
    assert responses[20].status_code == 429
    assert "Retry-After" in responses[20].headers
