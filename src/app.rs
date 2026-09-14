//! Application state, route table, and the non-license endpoints.

use std::sync::Arc;
use std::time::Instant;

use axum::{
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use serde_json::json;
use tower_http::limit::RequestBodyLimitLayer;

use crate::api::iso_z;
use crate::config::Config;

#[derive(Clone)]
pub struct AppState {
    pub pool: sqlx::PgPool,
    pub config: Arc<Config>,
    pub started_at: Instant,
}

pub const VERSION: &str = "0.1.0";

/// Check-in bodies carry three short optional strings. 64 KiB is orders
/// of magnitude more than that and still refuses anything pathological.
const MAX_BODY_BYTES: usize = 64 * 1024;

/// The whole route table, separated from `main` so integration tests can
/// drive it with `tower::ServiceExt::oneshot` rather than binding a port.
pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/", get(root))
        .route("/health", get(health))
        .route("/health/ready", get(health_ready))
        .route("/v1/licenses/check-in", post(crate::api::check_in))
        .route("/v1/licenses/entitlements", get(crate::api::entitlements))
        .layer(RequestBodyLimitLayer::new(MAX_BODY_BYTES))
        .with_state(state)
}

async fn root() -> Json<serde_json::Value> {
    Json(json!({
        "service": "sentinel-license-service",
        "time": iso_z(Utc::now().naive_utc()),
    }))
}

/// Pure liveness — must never be slow. This is what Fly's health check
/// polls; a slow dependency must not pull the only machine out of
/// rotation.
async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "healthy", "version": VERSION }))
}

/// Readiness — 503 if a critical probe fails, 200 otherwise.
async fn health_ready(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> (axum::http::StatusCode, Json<serde_json::Value>) {
    let started = Instant::now();
    let probe = sqlx::query("SELECT 1").execute(&state.pool).await;
    let latency_ms = (started.elapsed().as_secs_f64() * 100_000.0).round() / 100.0;

    let (ready, database) = match probe {
        Ok(_) => (true, json!({ "status": "ok", "latency_ms": latency_ms })),
        Err(err) => {
            tracing::error!(error = %err, "readiness: database probe failed");
            (
                false,
                json!({ "status": "critical", "error": err.to_string() }),
            )
        }
    };

    let status = if ready {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };

    (
        status,
        Json(json!({
            "ready": ready,
            "checks": { "database": database },
            "version": VERSION,
            "uptime_seconds": (state.started_at.elapsed().as_secs_f64() * 10.0).round() / 10.0,
        })),
    )
}
