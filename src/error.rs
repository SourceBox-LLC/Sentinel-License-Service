//! HTTP error shape.
//!
//! FastAPI's `HTTPException` renders as `{"detail": "..."}`, so this
//! reproduces that body shape rather than inventing a nicer one.
//!
//! There is exactly one 401 in this service (a missing or malformed
//! Authorization header) and otherwise no 4xx at all: an unknown, revoked
//! or expired key is a 200 with `valid: false`. See api.rs for why that
//! distinction is the whole contract.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub detail: String,
}

impl ApiError {
    pub fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
        }
    }

    /// Missing or malformed `Authorization` header. The message is copied
    /// verbatim from the Python service.
    pub fn unauthorized() -> Self {
        Self::new(
            StatusCode::UNAUTHORIZED,
            "Missing or malformed Authorization header",
        )
    }

    pub fn internal(detail: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, detail)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "detail": self.detail }))).into_response()
    }
}

/// A database failure is a 500 with a generic body — the underlying sqlx
/// error can name columns and constraints, which is not something to hand
/// to a caller. The detail goes to the log instead.
impl From<sqlx::Error> for ApiError {
    fn from(err: sqlx::Error) -> Self {
        tracing::error!(error = %err, "database error");
        ApiError::internal("database error")
    }
}
