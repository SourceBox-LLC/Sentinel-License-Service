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
    assert body["sync_enabled"] is False
    assert body["renews_at"] is not None


def test_checkin_reflects_sync_enabled(client, db_session):
    raw_key, _ = _make_license(db_session, sync_enabled=True)
    r = _check_in(client, raw_key)
    assert r.json()["sync_enabled"] is True


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


# ── Client IP resolution (spoof resistance) ─────────────────────────


def test_spoofed_x_forwarded_for_is_ignored_for_source_ip(client, db_session):
    # Regression: uvicorn's --forwarded-allow-ips=* trusts the leftmost
    # X-Forwarded-For entry verbatim, which a caller fully controls.
    # get_client_ip must not use it — only a Fly-set Fly-Client-IP (or
    # the raw TCP peer) is trustworthy.
    raw_key, license_row = _make_license(db_session)
    client.post(
        "/v1/licenses/check-in",
        json={"install_id": "x"},
        headers={
            "Authorization": f"Bearer {raw_key}",
            "X-Forwarded-For": "203.0.113.7",
        },
    )
    db_session.refresh(license_row)
    assert license_row.last_seen_ip != "203.0.113.7"


def test_fly_client_ip_header_is_used_when_present(client, db_session):
    raw_key, license_row = _make_license(db_session)
    client.post(
        "/v1/licenses/check-in",
        json={"install_id": "x"},
        headers={
            "Authorization": f"Bearer {raw_key}",
            "Fly-Client-IP": "198.51.100.42",
            "X-Forwarded-For": "203.0.113.7",  # must lose to Fly-Client-IP
        },
    )
    db_session.refresh(license_row)
    assert license_row.last_seen_ip == "198.51.100.42"


def test_spoofed_x_forwarded_for_cannot_bypass_rate_limit(client, db_session):
    # A different fake X-Forwarded-For on every request must not land
    # each request in a fresh rate-limit bucket.
    raw_key, _ = _make_license(db_session)
    responses = [
        client.post(
            "/v1/licenses/check-in",
            json={"install_id": "x"},
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Forwarded-For": f"203.0.113.{i}",
            },
        )
        for i in range(21)
    ]
    assert responses[20].status_code == 429


# ── Request validation ───────────────────────────────────────────────


def test_oversized_install_id_is_rejected(client, db_session):
    raw_key, _ = _make_license(db_session)
    r = client.post(
        "/v1/licenses/check-in",
        json={"install_id": "x" * 65},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 422


# ── Rate limiting ────────────────────────────────────────────────────


def test_check_in_is_rate_limited_per_ip(client, db_session):
    raw_key, _ = _make_license(db_session)

    responses = [_check_in(client, raw_key) for _ in range(21)]

    assert all(r.status_code == 200 for r in responses[:20])
    assert responses[20].status_code == 429
    assert "Retry-After" in responses[20].headers


# ── /v1/licenses/entitlements ────────────────────────────────────────


def _entitlements(client, raw_key):
    return client.get(
        "/v1/licenses/entitlements",
        headers={"Authorization": f"Bearer {raw_key}"},
    )


def test_entitlements_missing_auth_is_401(client):
    r = client.get("/v1/licenses/entitlements")
    assert r.status_code == 401


def test_entitlements_unknown_key_returns_200_valid_false(client):
    r = _entitlements(client, "slk_" + "0" * 32)
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["reason"] == "not_found"


def test_entitlements_for_active_license(client, db_session):
    raw_key, license_row = _make_license(
        db_session, tier="self_host_standard", monthly_run_cap=500, sync_enabled=True
    )
    r = _entitlements(client, raw_key)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["license_key_hash"] == license_row.key_hash
    assert body["tier"] == "self_host_standard"
    assert body["monthly_run_cap"] == 500
    assert body["sync_enabled"] is True


def test_entitlements_revoked_license_returns_valid_false(client, db_session):
    raw_key, _ = _make_license(db_session, status="revoked")
    r = _entitlements(client, raw_key)
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] == "revoked"
    assert body["license_key_hash"] is None


def test_entitlements_does_not_write_checkin_log(client, db_session):
    # This is the entire reason /entitlements exists as a separate
    # endpoint from /check-in — callers validating on every request
    # (e.g. Sentinel-Sync-Service) must not pollute the check-in audit
    # trail or fight Command Center's own check-in loop for rate-limit
    # headroom.
    raw_key, license_row = _make_license(db_session)
    _entitlements(client, raw_key)
    rows = db_session.query(LicenseCheckIn).filter_by(license_id=license_row.id).all()
    assert len(rows) == 0


def test_entitlements_does_not_update_last_seen(client, db_session):
    raw_key, license_row = _make_license(db_session)
    _entitlements(client, raw_key)
    db_session.refresh(license_row)
    assert license_row.last_seen_at is None


def test_entitlements_is_rate_limited_higher_than_checkin(client, db_session):
    raw_key, _ = _make_license(db_session)
    responses = [_entitlements(client, raw_key) for _ in range(61)]
    assert all(r.status_code == 200 for r in responses[:60])
    assert responses[60].status_code == 429
