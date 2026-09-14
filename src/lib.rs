//! Library surface so integration tests and the admin CLI can share the
//! same router, models and key handling the binary uses.

pub mod api;
pub mod app;
pub mod config;
pub mod error;
pub mod keys;

pub use app::{build_router, AppState, VERSION};
