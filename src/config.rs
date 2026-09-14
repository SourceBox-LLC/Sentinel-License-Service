//! Environment configuration.
//!
//! Names and defaults carried over from the Python service's
//! `app/core/config.py`. The Fly app's secrets are set against these
//! names, so renaming one silently reverts it to a default on deploy.

use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub database_url: String,
    pub port: u16,
}

fn var_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            database_url: normalize_database_url(&var_or(
                "DATABASE_URL",
                "postgresql://licensesvc:licensesvc@localhost:5432/licenses",
            )),
            port: var_or("PORT", "8000").parse().unwrap_or(8000),
        }
    }
}

/// Strip SQLAlchemy's `+driver` from a URL scheme.
///
/// The Fly secret still holds `postgresql+psycopg://...`, which sqlx does
/// not understand — normalise rather than require the secret be rewritten
/// during a deploy that is already changing the runtime.
pub fn normalize_database_url(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => match scheme.split_once('+') {
            Some((base, _driver)) => format!("{base}://{rest}"),
            None => url.to_string(),
        },
        None => url.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_database_url;

    #[test]
    fn strips_the_sqlalchemy_driver_suffix() {
        assert_eq!(
            normalize_database_url("postgresql+psycopg://u:p@h:5432/db"),
            "postgresql://u:p@h:5432/db"
        );
    }

    #[test]
    fn leaves_a_plain_url_and_a_plus_in_the_password_alone() {
        for url in ["postgres://u:p@h/db", "postgresql://user:pa+ss@h/db"] {
            assert_eq!(normalize_database_url(url), url);
        }
    }
}
