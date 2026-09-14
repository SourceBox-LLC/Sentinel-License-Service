//! The wire contract, as tests.
//!
//! Ported from Python; what could break silently is not "does it
//! compile" but "does it still answer exactly what Command Center's
//! license_client.py and Sentinel-Sync-Service's entitlement check
//! expect". During the port both implementations ran against one database
//! and were diffed on responses AND database side effects — 25/25
//! responses identical, 0 side-effect mismatches. These carry the cases
//! that mattered, now that the Python is gone.
//!
//! Needs a Postgres: set TEST_DATABASE_URL (or DATABASE_URL). Skipped
//! with a message otherwise, so `cargo test` on a laptop still passes
//! rather than failing for the wrong reason.

use http_body_util::BodyExt;
use serde_json::{json, Value};
use sqlx::postgres::PgPoolOptions;
use tower::ServiceExt;

use sentinel_license_service::config::normalize_database_url;
use sentinel_license_service::keys::hash_key;
use sentinel_license_service::{build_router, AppState};

fn database_url() -> Option<String> {
    std::env::var("TEST_DATABASE_URL")
        .or_else(|_| std::env::var("DATABASE_URL"))
        .ok()
        .map(|u| normalize_database_url(&u))
}

async fn state() -> Option<AppState> {
    let url = database_url()?;
    let pool = PgPoolOptions::new()
        .max_connections(4)
        .connect(&url)
        .await
        .ok()?;
    sqlx::migrate!("./migrations").run(&pool).await.unwrap();
    Some(AppState {
        pool,
        config: std::sync::Arc::new(sentinel_license_service::config::Config::from_env()),
        started_at: std::time::Instant::now(),
    })
}

/// Each test owns a key suffix so they can share a database without
/// truncating each other's rows.
async fn seed(
    st: &AppState,
    suffix: &str,
    status: &str,
    renews_days: Option<i64>,
    sync: bool,
) -> String {
    let raw = format!("slk_test_{suffix}");
    sqlx::query("DELETE FROM license_checkins WHERE key_hash = $1")
        .bind(hash_key(&raw))
        .execute(&st.pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM licenses WHERE key_hash = $1")
        .bind(hash_key(&raw))
        .execute(&st.pool)
        .await
        .unwrap();
    let renews = renews_days.map(|d| chrono::Utc::now().naive_utc() + chrono::Duration::days(d));
    sqlx::query(
        "INSERT INTO licenses
           (key_hash,key_last4,tier,monthly_run_cap,sync_enabled,status,issued_at,renews_at,issued_by)
         VALUES ($1,$2,'tier_x',777,$3,$4,$5,$6,'test')",
    )
    .bind(hash_key(&raw))
    .bind(&raw[raw.len() - 4..])
    .bind(sync)
    .bind(status)
    .bind(chrono::Utc::now().naive_utc())
    .bind(renews)
    .execute(&st.pool)
    .await
    .unwrap();
    raw
}

async fn call(
    st: &AppState,
    method: &str,
    uri: &str,
    auth: Option<&str>,
    body: Option<Value>,
) -> (u16, Value) {
    let mut req = axum::http::Request::builder().method(method).uri(uri);
    if let Some(a) = auth {
        req = req.header("authorization", a);
    }
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(axum::body::Body::from(serde_json::to_vec(&b).unwrap()))
            .unwrap(),
        None => req.body(axum::body::Body::empty()).unwrap(),
    };
    let resp = build_router(st.clone()).oneshot(req).await.unwrap();
    let status = resp.status().as_u16();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

macro_rules! st {
    () => {
        match state().await {
            Some(s) => s,
            None => {
                eprintln!("skipping: set TEST_DATABASE_URL");
                return;
            }
        }
    };
}

// --- THE contract ----------------------------------------------------

#[tokio::test]
async fn an_answerable_question_is_always_200_even_when_the_answer_is_no() {
    // This is the whole contract. A caller must be able to tell "the
    // service said no" (apply immediately) from "I couldn't reach it"
    // (apply grace). Returning 4xx for a revoked key would make an
    // install fall into its grace window instead of locking out.
    let st = st!();
    for (suffix, status) in [
        ("revoked", "revoked"),
        ("susp", "suspended"),
        ("gremlin", "some_future_status"),
    ] {
        let key = seed(&st, suffix, status, Some(365), true).await;
        let (code, body) = call(
            &st,
            "POST",
            "/v1/licenses/check-in",
            Some(&format!("Bearer {key}")),
            Some(json!({})),
        )
        .await;
        assert_eq!(code, 200, "{status} must be 200, not an error");
        assert_eq!(body["valid"], false);
        assert_eq!(body["reason"], status, "reason echoes the status verbatim");
    }
}

#[tokio::test]
async fn an_unknown_key_is_200_not_found_not_401() {
    let st = st!();
    let (code, body) = call(
        &st,
        "POST",
        "/v1/licenses/check-in",
        Some("Bearer slk_never_issued"),
        Some(json!({})),
    )
    .await;
    assert_eq!(code, 200);
    assert_eq!(body["valid"], false);
    assert_eq!(body["reason"], "not_found");
}

#[tokio::test]
async fn only_a_malformed_header_is_a_401() {
    let st = st!();
    for header in [None, Some("abc"), Some("Basic x"), Some("Bearer   ")] {
        let (code, body) = call(&st, "GET", "/v1/licenses/entitlements", header, None).await;
        assert_eq!(code, 401, "{header:?}");
        assert_eq!(body["detail"], "Missing or malformed Authorization header");
    }
}

// --- verdict ordering -------------------------------------------------

#[tokio::test]
async fn a_lapsed_renewal_on_an_active_license_is_expired() {
    let st = st!();
    let key = seed(&st, "lapsed", "active", Some(-1), true).await;
    let (_, body) = call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;
    assert_eq!(body["valid"], false);
    assert_eq!(body["reason"], "expired");
}

#[tokio::test]
async fn status_wins_over_expiry() {
    // A revoked key whose renewal also lapsed reports "revoked" — the
    // Python ordering, which matters because the reason is surfaced to
    // the operator.
    let st = st!();
    let key = seed(&st, "revlapse", "revoked", Some(-1), true).await;
    let (_, body) = call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;
    assert_eq!(body["reason"], "revoked");
}

#[tokio::test]
async fn a_null_renewal_never_expires() {
    let st = st!();
    let key = seed(&st, "perpetual", "active", None, true).await;
    let (_, body) = call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;
    assert_eq!(body["valid"], true);
    assert!(body["reason"].is_null());
}

// --- the fields callers read ------------------------------------------

#[tokio::test]
async fn entitlements_returns_the_tenant_key_sync_service_scopes_on() {
    // Sentinel-Sync-Service partitions every mirrored row by this. A null
    // here makes it 502 rather than silently mixing tenants.
    let st = st!();
    let key = seed(&st, "tenant", "active", Some(365), true).await;
    let (code, body) = call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;
    assert_eq!(code, 200);
    assert_eq!(body["valid"], true);
    assert_eq!(body["license_key_hash"], hash_key(&key));
    assert_eq!(body["sync_enabled"], true);
    assert_eq!(body["monthly_run_cap"], 777);
}

#[tokio::test]
async fn sync_disabled_is_reported_as_false_not_omitted() {
    let st = st!();
    let key = seed(&st, "nosync", "active", Some(365), false).await;
    let (_, body) = call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;
    assert_eq!(body["sync_enabled"], false);
    assert!(body.as_object().unwrap().contains_key("sync_enabled"));
}

#[tokio::test]
async fn timestamps_carry_the_trailing_z_callers_parse() {
    // Naive ISO plus a literal Z — Python's `isoformat() + "Z"`. chrono's
    // own serde impl would omit the Z, and an offset-aware type would
    // write +00:00 instead.
    let st = st!();
    let key = seed(&st, "stamps", "active", Some(365), true).await;
    let (_, body) = call(
        &st,
        "POST",
        "/v1/licenses/check-in",
        Some(&format!("Bearer {key}")),
        Some(json!({})),
    )
    .await;
    for field in ["server_time", "renews_at"] {
        let v = body[field]
            .as_str()
            .unwrap_or_else(|| panic!("{field} missing"));
        assert!(v.ends_with('Z'), "{field} must end with Z: {v}");
        assert!(!v.contains('+'), "{field} must not be offset-aware: {v}");
        assert_eq!(v.len(), "2026-09-14T05:57:38.281249Z".len(), "{field}: {v}");
    }
}

// --- side effects -----------------------------------------------------

#[tokio::test]
async fn check_in_writes_an_audit_row_and_touches_last_seen() {
    let st = st!();
    let key = seed(&st, "audit", "active", Some(365), true).await;
    call(
        &st,
        "POST",
        "/v1/licenses/check-in",
        Some(&format!("Bearer {key}")),
        Some(json!({"install_id":"inst-1"})),
    )
    .await;

    let (result, install): (String, Option<String>) =
        sqlx::query_as("SELECT result, install_id FROM license_checkins WHERE key_hash = $1")
            .bind(hash_key(&key))
            .fetch_one(&st.pool)
            .await
            .unwrap();
    assert_eq!(result, "ok");
    assert_eq!(install.as_deref(), Some("inst-1"));

    let (seen,): (Option<chrono::NaiveDateTime>,) =
        sqlx::query_as("SELECT last_seen_at FROM licenses WHERE key_hash = $1")
            .bind(hash_key(&key))
            .fetch_one(&st.pool)
            .await
            .unwrap();
    assert!(seen.is_some(), "last_seen_at must be set");
}

#[tokio::test]
async fn a_revoked_key_still_updates_last_seen() {
    // An install that hasn't noticed it's revoked is a useful signal, so
    // the write happens regardless of the verdict.
    let st = st!();
    let key = seed(&st, "revseen", "revoked", Some(365), true).await;
    call(
        &st,
        "POST",
        "/v1/licenses/check-in",
        Some(&format!("Bearer {key}")),
        Some(json!({})),
    )
    .await;
    let (seen,): (Option<chrono::NaiveDateTime>,) =
        sqlx::query_as("SELECT last_seen_at FROM licenses WHERE key_hash = $1")
            .bind(hash_key(&key))
            .fetch_one(&st.pool)
            .await
            .unwrap();
    assert!(seen.is_some());
}

#[tokio::test]
async fn an_unknown_key_is_logged_with_a_null_license_id() {
    let st = st!();
    let raw = "slk_ghost_key_xyz";
    sqlx::query("DELETE FROM license_checkins WHERE key_hash = $1")
        .bind(hash_key(raw))
        .execute(&st.pool)
        .await
        .unwrap();
    call(
        &st,
        "POST",
        "/v1/licenses/check-in",
        Some(&format!("Bearer {raw}")),
        Some(json!({})),
    )
    .await;
    let (result, lid): (String, Option<i32>) =
        sqlx::query_as("SELECT result, license_id FROM license_checkins WHERE key_hash = $1")
            .bind(hash_key(raw))
            .fetch_one(&st.pool)
            .await
            .unwrap();
    assert_eq!(result, "not_found");
    assert!(lid.is_none());
}

#[tokio::test]
async fn entitlements_writes_nothing() {
    // It exists precisely so Sync can validate on every request without
    // polluting the check-in audit trail or moving last_seen.
    let st = st!();
    let key = seed(&st, "readonly", "active", Some(365), true).await;
    call(
        &st,
        "GET",
        "/v1/licenses/entitlements",
        Some(&format!("Bearer {key}")),
        None,
    )
    .await;

    let (n,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM license_checkins WHERE key_hash = $1")
        .bind(hash_key(&key))
        .fetch_one(&st.pool)
        .await
        .unwrap();
    assert_eq!(n, 0, "entitlements must not write an audit row");

    let (seen,): (Option<chrono::NaiveDateTime>,) =
        sqlx::query_as("SELECT last_seen_at FROM licenses WHERE key_hash = $1")
            .bind(hash_key(&key))
            .fetch_one(&st.pool)
            .await
            .unwrap();
    assert!(seen.is_none(), "entitlements must not move last_seen_at");
}
