//! License check-in and entitlement lookup.
//!
//! THE CONTRACT, and it is subtle enough to restate: these endpoints
//! return HTTP 200 whenever they can actually determine a key's validity,
//! with `valid: bool` in the body — including for a key that is revoked,
//! expired, suspended, or never issued. That is the only way a caller can
//! tell "the service answered and said no" (apply immediately, no grace)
//! from "I could not reach it" (apply the grace window).
//!
//! So a 4xx/5xx here means something the service genuinely could not
//! answer: a malformed Authorization header, a rate-limit trip, or a
//! database failure. Never blur that line by returning an error for a
//! question the service can answer — Command Center's license_client.py
//! treats any non-200 as "unreachable", and a caller that cannot get a
//! trustworthy verdict must never be told `valid: false` as if it had.

use axum::{extract::State, http::HeaderMap, Json};
use chrono::{NaiveDateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::app::AppState;
use crate::error::ApiError;
use crate::keys::hash_key;

#[derive(Debug, Deserialize, Default)]
pub struct CheckInRequest {
    #[serde(default)]
    pub install_id: Option<String>,
    #[serde(default)]
    pub product: Option<String>,
    #[serde(default)]
    pub client_version: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CheckInResponse {
    pub valid: bool,
    pub reason: Option<String>,
    pub tier: Option<String>,
    pub status: Option<String>,
    pub monthly_run_cap: Option<i32>,
    pub sync_enabled: bool,
    pub renews_at: Option<String>,
    pub server_time: String,
}

#[derive(Debug, Serialize)]
pub struct EntitlementsResponse {
    pub valid: bool,
    pub reason: Option<String>,
    /// Stable, server-derived tenant identifier — NOT the caller-asserted
    /// install_id, which a client can set to anything. Sentinel-Sync-Service
    /// scopes every mirrored row by this.
    pub license_key_hash: Option<String>,
    pub tier: Option<String>,
    pub monthly_run_cap: Option<i32>,
    pub sync_enabled: bool,
    pub server_time: String,
}

/// Naive UTC plus a literal trailing `Z`, matching Python's
/// `datetime.isoformat() + "Z"`. Note this is a *string* field on the
/// wire, not a serialised datetime — chrono's serde impl would render it
/// without the Z and callers parse the Z.
pub fn iso_z(dt: NaiveDateTime) -> String {
    format!("{}Z", dt.format("%Y-%m-%dT%H:%M:%S%.6f"))
}

/// `Authorization: Bearer <key>`, or a 401. The only 401 in this service.
fn raw_key_from(headers: &HeaderMap) -> Result<String, ApiError> {
    let header = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .ok_or_else(ApiError::unauthorized)?;
    let mut parts = header.splitn(2, char::is_whitespace);
    let scheme = parts.next().ok_or_else(ApiError::unauthorized)?;
    if !scheme.eq_ignore_ascii_case("bearer") {
        return Err(ApiError::unauthorized());
    }
    let rest = parts.next().ok_or_else(ApiError::unauthorized)?.trim();
    if rest.is_empty() {
        return Err(ApiError::unauthorized());
    }
    Ok(rest.to_string())
}

/// The row shape both endpoints read.
#[derive(Debug, sqlx::FromRow)]
struct LicenseRow {
    id: i32,
    key_hash: String,
    tier: String,
    monthly_run_cap: i32,
    sync_enabled: bool,
    status: String,
    renews_at: Option<NaiveDateTime>,
}

/// Allow-list, not deny-list: any status other than "active" — a typo,
/// data corruption, or a future value nothing here recognises yet — must
/// fail closed rather than pass through as valid.
fn verdict(row: &LicenseRow, now: NaiveDateTime) -> String {
    if row.status != "active" {
        row.status.clone()
    } else if row.renews_at.is_some_and(|r| r < now) {
        "expired".to_string()
    } else {
        "ok".to_string()
    }
}

async fn find_license(
    pool: &sqlx::PgPool,
    key_hash: &str,
) -> Result<Option<LicenseRow>, sqlx::Error> {
    sqlx::query_as::<_, LicenseRow>(
        "SELECT id, key_hash, tier, monthly_run_cap, sync_enabled, status, renews_at
           FROM licenses
          WHERE key_hash = $1",
    )
    .bind(key_hash)
    .fetch_optional(pool)
    .await
}

/// Client IP, preferring Fly's forwarded header. Stored on the license row
/// and the audit trail.
fn client_ip(headers: &HeaderMap) -> Option<String> {
    headers
        .get("fly-client-ip")
        .or_else(|| headers.get("x-forwarded-for"))
        .and_then(|v| v.to_str().ok())
        .map(|v| v.split(',').next().unwrap_or(v).trim().to_string())
        .filter(|s| !s.is_empty())
}

pub async fn check_in(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Option<Json<CheckInRequest>>,
) -> Result<Json<CheckInResponse>, ApiError> {
    let raw_key = raw_key_from(&headers)?;
    let key_hash = hash_key(&raw_key);
    let now = Utc::now().naive_utc();
    let source_ip = client_ip(&headers);
    let payload = body.map(|Json(b)| b).unwrap_or_default();

    // Bounded to the column widths these are stored in. SQLite never
    // enforced VARCHAR(N), so the validation layer was the only cap —
    // Postgres would reject instead, turning a sloppy client into a 500.
    let install_id = payload.install_id.as_deref().map(|s| truncate(s, 64));

    let license = find_license(&state.pool, &key_hash).await?;

    let Some(row) = license else {
        log_checkin(
            &state.pool,
            &key_hash,
            None,
            source_ip.as_deref(),
            install_id.as_deref(),
            "not_found",
            now,
        )
        .await?;
        return Ok(Json(CheckInResponse {
            valid: false,
            reason: Some("not_found".into()),
            tier: None,
            status: None,
            monthly_run_cap: None,
            sync_enabled: false,
            renews_at: None,
            server_time: iso_z(now),
        }));
    };

    let result = verdict(&row, now);

    // One transaction for both writes: the Python version committed once,
    // so there was no window where last_seen updated durably while the
    // audit row silently failed.
    let mut tx = state.pool.begin().await?;

    // Update last-seen regardless of outcome — a revoked key still
    // checking in is a useful signal (an install that hasn't noticed).
    sqlx::query("UPDATE licenses SET last_seen_at = $1, last_seen_ip = $2 WHERE id = $3")
        .bind(now)
        .bind(source_ip.as_deref())
        .bind(row.id)
        .execute(&mut *tx)
        .await?;

    sqlx::query(
        "INSERT INTO license_checkins
             (key_hash, license_id, checked_in_at, source_ip, install_id, result)
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(&key_hash)
    .bind(Some(row.id))
    .bind(now)
    .bind(source_ip.as_deref())
    .bind(install_id.as_deref())
    .bind(&result)
    .execute(&mut *tx)
    .await?;

    tx.commit().await?;

    if result != "ok" {
        return Ok(Json(CheckInResponse {
            valid: false,
            reason: Some(result),
            tier: None,
            status: None,
            monthly_run_cap: None,
            sync_enabled: false,
            renews_at: None,
            server_time: iso_z(now),
        }));
    }

    Ok(Json(CheckInResponse {
        valid: true,
        reason: None,
        tier: Some(row.tier),
        status: Some(row.status),
        monthly_run_cap: Some(row.monthly_run_cap),
        sync_enabled: row.sync_enabled,
        renews_at: row.renews_at.map(iso_z),
        server_time: iso_z(now),
    }))
}

/// Read-only entitlement lookup.
///
/// Deliberately separate from check-in: it touches neither last_seen nor
/// the audit trail, so other services (Sentinel-Sync-Service validating a
/// push) can call it freely without polluting the check-in history or
/// competing with Command Center's 15-minute cadence for rate-limit
/// headroom.
pub async fn entitlements(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<EntitlementsResponse>, ApiError> {
    let raw_key = raw_key_from(&headers)?;
    let key_hash = hash_key(&raw_key);
    let now = Utc::now().naive_utc();

    let Some(row) = find_license(&state.pool, &key_hash).await? else {
        return Ok(Json(EntitlementsResponse {
            valid: false,
            reason: Some("not_found".into()),
            license_key_hash: None,
            tier: None,
            monthly_run_cap: None,
            sync_enabled: false,
            server_time: iso_z(now),
        }));
    };

    let result = verdict(&row, now);
    if result != "ok" {
        return Ok(Json(EntitlementsResponse {
            valid: false,
            reason: Some(result),
            license_key_hash: None,
            tier: None,
            monthly_run_cap: None,
            sync_enabled: false,
            server_time: iso_z(now),
        }));
    }

    Ok(Json(EntitlementsResponse {
        valid: true,
        reason: None,
        license_key_hash: Some(row.key_hash),
        tier: Some(row.tier),
        monthly_run_cap: Some(row.monthly_run_cap),
        sync_enabled: row.sync_enabled,
        server_time: iso_z(now),
    }))
}

async fn log_checkin(
    pool: &sqlx::PgPool,
    key_hash: &str,
    license_id: Option<i32>,
    source_ip: Option<&str>,
    install_id: Option<&str>,
    result: &str,
    now: NaiveDateTime,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO license_checkins
             (key_hash, license_id, checked_in_at, source_ip, install_id, result)
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(key_hash)
    .bind(license_id)
    .bind(now)
    .bind(source_ip)
    .bind(install_id)
    .bind(result)
    .execute(pool)
    .await?;
    Ok(())
}

fn truncate(s: &str, max_chars: usize) -> String {
    s.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn row(status: &str, renews_at: Option<NaiveDateTime>) -> LicenseRow {
        LicenseRow {
            id: 1,
            key_hash: "h".into(),
            tier: "self_host_standard".into(),
            monthly_run_cap: 500,
            sync_enabled: false,
            status: status.into(),
            renews_at,
        }
    }

    fn at(y: i32, m: u32, d: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap()
    }

    #[test]
    fn active_and_unexpired_is_ok() {
        let now = at(2026, 9, 14);
        assert_eq!(verdict(&row("active", None), now), "ok");
        assert_eq!(verdict(&row("active", Some(at(2027, 1, 1))), now), "ok");
    }

    #[test]
    fn a_past_renewal_is_expired() {
        let now = at(2026, 9, 14);
        assert_eq!(
            verdict(&row("active", Some(at(2026, 9, 13))), now),
            "expired"
        );
    }

    #[test]
    fn any_non_active_status_fails_closed() {
        let now = at(2026, 9, 14);
        // Including a status this code has never heard of — allow-list.
        for status in ["revoked", "suspended", "some_future_status", ""] {
            assert_eq!(verdict(&row(status, None), now), status);
        }
    }

    #[test]
    fn status_is_reported_before_expiry_is_considered() {
        // A revoked key whose renewal also lapsed reports "revoked", not
        // "expired" — the Python ordering, preserved.
        let now = at(2026, 9, 14);
        assert_eq!(
            verdict(&row("revoked", Some(at(2020, 1, 1))), now),
            "revoked"
        );
    }

    #[test]
    fn iso_z_matches_python_isoformat_plus_z() {
        let dt = NaiveDate::from_ymd_opt(2026, 9, 14)
            .unwrap()
            .and_hms_micro_opt(5, 57, 38, 281249)
            .unwrap();
        assert_eq!(iso_z(dt), "2026-09-14T05:57:38.281249Z");
    }
}
