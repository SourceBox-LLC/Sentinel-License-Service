//! Sentinel-License-Service — license issuance and check-in.
//!
//! Ported from the Python/FastAPI implementation. The wire contract is
//! the specification: Command Center's license_client.py polls /check-in
//! and Sentinel-Sync-Service validates every push against /entitlements.
//!
//! Serving is the default. The subcommands replace the Python operator
//! scripts (issue_license.py / manage_license.py), so the image no longer
//! needs a Python runtime to be administrable — `fly ssh console -C
//! "sentinel-license-service show --key slk_..."` works on the machine.

use std::sync::Arc;
use std::time::Instant;

use clap::{Parser, Subcommand};
use sqlx::postgres::PgPoolOptions;

use sentinel_license_service::api::iso_z;
use sentinel_license_service::config::Config;
use sentinel_license_service::keys::{generate_key, hash_key, last4};
use sentinel_license_service::{build_router, AppState, VERSION};

#[derive(Parser)]
#[command(name = "sentinel-license-service", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Issue a new license key. The raw key is printed ONCE and never
    /// stored — only its SHA-256 hash is.
    Issue {
        #[arg(long)]
        email: Option<String>,
        #[arg(long)]
        label: Option<String>,
        #[arg(long, default_value = "self_host_standard")]
        tier: String,
        #[arg(long, default_value_t = 500)]
        monthly_run_cap: i32,
        #[arg(long)]
        renews_days: Option<i64>,
        #[arg(long, default_value_t = false)]
        sync_enabled: bool,
        #[arg(long, default_value = "manual")]
        issued_by: String,
        #[arg(long)]
        notes: Option<String>,
    },
    /// Show a license by raw key or id.
    Show {
        #[arg(long)]
        key: Option<String>,
        #[arg(long)]
        id: Option<i32>,
    },
    /// List licenses, newest first.
    List {
        #[arg(long, default_value_t = 50)]
        limit: i64,
    },
    /// Set status to revoked. Takes effect on the next check-in.
    Revoke {
        #[arg(long)]
        key: Option<String>,
        #[arg(long)]
        id: Option<i32>,
    },
    /// Set status to suspended (reversible).
    Suspend {
        #[arg(long)]
        key: Option<String>,
        #[arg(long)]
        id: Option<i32>,
    },
    /// Set status back to active.
    Reactivate {
        #[arg(long)]
        key: Option<String>,
        #[arg(long)]
        id: Option<i32>,
    },
    /// Turn the cloud-sync entitlement on or off.
    SetSync {
        #[arg(long)]
        key: Option<String>,
        #[arg(long)]
        id: Option<i32>,
        #[arg(long)]
        enabled: bool,
    },
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // Operator commands print a report; sqlx's "relation already exists,
    // skipping" notices on top of it are noise in a terminal. The server
    // wants them. RUST_LOG still overrides either way.
    let default_level = if cli.command.is_some() {
        "warn"
    } else {
        "info"
    };
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| default_level.into()),
        )
        .init();

    let config = Config::from_env();
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&config.database_url)
        .await?;

    // Schema bring-up, replacing the Python service's boot-time
    // create_all()/sync_schema(). sqlx holds a Postgres advisory lock for
    // the duration, and the migration is IF NOT EXISTS throughout, so it
    // is a no-op against the table SQLAlchemy already created.
    sqlx::migrate!("./migrations").run(&pool).await?;

    match cli.command {
        None => serve(config, pool).await,
        Some(cmd) => admin(cmd, &pool).await,
    }
}

async fn serve(config: Config, pool: sqlx::PgPool) -> anyhow::Result<()> {
    let port = config.port;
    let state = AppState {
        pool,
        config: Arc::new(config),
        started_at: Instant::now(),
    };

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    tracing::info!(
        port,
        version = VERSION,
        "sentinel-license-service listening"
    );

    axum::serve(listener, build_router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutting down");
        })
        .await?;
    Ok(())
}

/// The columns `show` prints. Named rather than an inline tuple so the
/// signature stays readable.
type ShowRow = (
    i32,                           // id
    String,                        // key_last4
    String,                        // tier
    String,                        // status
    i32,                           // monthly_run_cap
    bool,                          // sync_enabled
    String,                        // issued_by
    Option<String>,                // customer_email
    Option<chrono::NaiveDateTime>, // renews_at
    Option<chrono::NaiveDateTime>, // last_seen_at
);

/// Resolve a license id from either `--key` (hashed, never stored) or
/// `--id`. Exactly one is required.
async fn resolve_id(
    pool: &sqlx::PgPool,
    key: Option<String>,
    id: Option<i32>,
) -> anyhow::Result<i32> {
    match (key, id) {
        (Some(k), None) => {
            let hash = hash_key(&k);
            let found: Option<(i32,)> =
                sqlx::query_as("SELECT id FROM licenses WHERE key_hash = $1")
                    .bind(&hash)
                    .fetch_optional(pool)
                    .await?;
            found
                .map(|(i,)| i)
                .ok_or_else(|| anyhow::anyhow!("no license matches that key"))
        }
        (None, Some(i)) => Ok(i),
        _ => anyhow::bail!("pass exactly one of --key or --id"),
    }
}

async fn set_status(pool: &sqlx::PgPool, id: i32, status: &str) -> anyhow::Result<()> {
    let done = sqlx::query("UPDATE licenses SET status = $1 WHERE id = $2")
        .bind(status)
        .bind(id)
        .execute(pool)
        .await?;
    if done.rows_affected() == 0 {
        anyhow::bail!("no license with id {id}");
    }
    println!("license {id}: status -> {status}");
    Ok(())
}

async fn admin(cmd: Command, pool: &sqlx::PgPool) -> anyhow::Result<()> {
    match cmd {
        Command::Issue {
            email,
            label,
            tier,
            monthly_run_cap,
            renews_days,
            sync_enabled,
            issued_by,
            notes,
        } => {
            let raw = generate_key();
            let now = chrono::Utc::now().naive_utc();
            let renews_at = renews_days.map(|d| now + chrono::Duration::days(d));

            let (id,): (i32,) = sqlx::query_as(
                "INSERT INTO licenses
                   (key_hash, key_last4, tier, monthly_run_cap, sync_enabled, status,
                    customer_email, customer_label, issued_at, renews_at, issued_by, notes)
                 VALUES ($1,$2,$3,$4,$5,'active',$6,$7,$8,$9,$10,$11)
                 RETURNING id",
            )
            .bind(hash_key(&raw))
            .bind(last4(&raw))
            .bind(&tier)
            .bind(monthly_run_cap)
            .bind(sync_enabled)
            .bind(email.as_deref())
            .bind(label.as_deref())
            .bind(now)
            .bind(renews_at)
            .bind(&issued_by)
            .bind(notes.as_deref())
            .fetch_one(pool)
            .await?;

            println!("license {id} issued");
            println!("  tier            {tier}");
            println!("  monthly_run_cap {monthly_run_cap}");
            println!("  sync_enabled    {sync_enabled}");
            match renews_at {
                Some(r) => println!("  renews_at       {}", iso_z(r)),
                None => println!("  renews_at       (never)"),
            }
            println!("\n  KEY (shown once, not stored): {raw}");
            Ok(())
        }
        Command::Show { key, id } => {
            let id = resolve_id(pool, key, id).await?;
            let row: Option<ShowRow> = sqlx::query_as(
                "SELECT id, key_last4, tier, status, monthly_run_cap, sync_enabled,
                            issued_by, customer_email, renews_at, last_seen_at
                       FROM licenses WHERE id = $1",
            )
            .bind(id)
            .fetch_optional(pool)
            .await?;
            let Some(r) = row else {
                anyhow::bail!("no license with id {id}");
            };
            println!("license {}", r.0);
            println!("  key           ...{}", r.1);
            println!("  tier          {}", r.2);
            println!("  status        {}", r.3);
            println!("  run cap       {}", r.4);
            println!("  sync_enabled  {}", r.5);
            println!("  issued_by     {}", r.6);
            println!("  email         {}", r.7.unwrap_or_else(|| "-".into()));
            println!(
                "  renews_at     {}",
                r.8.map(iso_z).unwrap_or_else(|| "(never)".into())
            );
            println!(
                "  last_seen_at  {}",
                r.9.map(iso_z).unwrap_or_else(|| "(never)".into())
            );
            Ok(())
        }
        Command::List { limit } => {
            let rows: Vec<(
                i32,
                String,
                String,
                String,
                bool,
                Option<chrono::NaiveDateTime>,
            )> = sqlx::query_as(
                "SELECT id, key_last4, tier, status, sync_enabled, last_seen_at
                       FROM licenses ORDER BY id DESC LIMIT $1",
            )
            .bind(limit)
            .fetch_all(pool)
            .await?;
            println!(
                "{:>4}  {:<8} {:<22} {:<10} {:<5} last seen",
                "id", "key", "tier", "status", "sync"
            );
            for r in rows {
                println!(
                    "{:>4}  ...{:<5} {:<22} {:<10} {:<5} {}",
                    r.0,
                    r.1,
                    r.2,
                    r.3,
                    r.4,
                    r.5.map(iso_z).unwrap_or_else(|| "-".into())
                );
            }
            Ok(())
        }
        Command::Revoke { key, id } => {
            let id = resolve_id(pool, key, id).await?;
            set_status(pool, id, "revoked").await
        }
        Command::Suspend { key, id } => {
            let id = resolve_id(pool, key, id).await?;
            set_status(pool, id, "suspended").await
        }
        Command::Reactivate { key, id } => {
            let id = resolve_id(pool, key, id).await?;
            set_status(pool, id, "active").await
        }
        Command::SetSync { key, id, enabled } => {
            let id = resolve_id(pool, key, id).await?;
            let done = sqlx::query("UPDATE licenses SET sync_enabled = $1 WHERE id = $2")
                .bind(enabled)
                .bind(id)
                .execute(pool)
                .await?;
            if done.rows_affected() == 0 {
                anyhow::bail!("no license with id {id}");
            }
            println!("license {id}: sync_enabled -> {enabled}");
            Ok(())
        }
    }
}
