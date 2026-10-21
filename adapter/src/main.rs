//! mc-adapter: AstrBot 与 Magic Context 之间的本地 HTTP 适配层。
//!
//! 只做协议转发：接收 CK v2 transform 请求，调用 mc-module 的 transform，
//! 把结果原样返回。不做任何压缩/丢弃策略决策（策略在 mc 内部）。

use std::{
    net::SocketAddr,
    path::PathBuf,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use cortexkit_store_types::{Isolation, StorageBackend, StorageDescriptor};
use mc_module::transform;
use mc_store::single_store_domain::{create_test_context_db, SqliteContextDomain};
use mc_store::McStore;
use serde_json::{json, Value};

struct App {
    store: Arc<McStore>,
    store_dir: PathBuf,
    started_at_ms: u128,
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

fn descriptor(dir: &PathBuf) -> StorageDescriptor {
    StorageDescriptor {
        module_id: "magic-context".to_string(),
        storage_namespace: "mc_cache".to_string(),
        isolation: Isolation::Module,
        backend: StorageBackend::Sqlite {
            path: dir.join("store.db").to_string_lossy().into_owned(),
        },
    }
}

/// 构造要返回给调用方的响应：transform 的完整 JSON + 原生消息。
fn transform_response(result: &transform::TransformResponse) -> Value {
    match serde_json::to_value(result) {
        Ok(v) => v,
        Err(e) => json!({ "status": "error", "error": format!("serialize response: {e}") }),
    }
}

async fn health(State(app): State<Arc<App>>) -> impl IntoResponse {
    Json(json!({
        "ok": true,
        "service": "mc-adapter",
        "version": env!("CARGO_PKG_VERSION"),
        "store_dir": app.store_dir.to_string_lossy(),
        "uptime_ms": now_ms().saturating_sub(app.started_at_ms),
    }))
}

async fn do_transform(
    State(app): State<Arc<App>>,
    body: String,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let raw: Value = serde_json::from_str(&body).map_err(|e| {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({ "status": "error", "error": format!("invalid json: {e}") })),
        )
    })?;

    let request: transform::TransformRequest =
        serde_json::from_value(raw).map_err(|e| {
            (
                StatusCode::BAD_REQUEST,
                Json(json!({ "status": "error", "error": format!("invalid transform request: {e}") })),
            )
        })?;

    let project_dir = app.store_dir.to_string_lossy().into_owned();
    let ctx = transform::ProducerContext {
        project_path: "git:astrbot",
        note_project_path: "git:astrbot",
        project_directory: &project_dir,
        history_budget_tokens: 60_000.0,
        memory_budget_tokens: 8_000.0,
        user_profile_budget_tokens: 4_000.0,
        memory_enabled: true,
        inject_docs: false,
        temporal_awareness: true,
        now_ms: now_ms() as i64,
        execute_threshold_percentage: 65.0,
        protected_tokens_floor: 16_000,
        protected_tokens_provenance: "derived",
        compaction_enabled: true,
        smart_drops: true,
        protected_tools: Default::default(),
        cache_ttl: "5m".into(),
        cache_ttl_provenance: mc_module::config::CacheTtlProvenance::Default,
        cache_ttl_policy: None,
        model_key: None,
        observed_last_response_at_ms: None,
        guidance_date: None,
        historian_active: false,
        wrapup_active: false,
        caveman_english_word_rules: true,
    };

    match transform::transform(&app.store, &request, &ctx) {
        Ok(result) => Ok(Json(transform_response(&result))),
        Err(e) => Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "status": "error", "error": format!("transform failed: {e:?}") })),
        )),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "mc_adapter=info".into()),
        )
        .init();

    let port: u16 = std::env::var("MC_ADAPTER_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8317);
    let store_dir = PathBuf::from(
        std::env::var("MC_STORE_DIR").unwrap_or_else(|_| "./data".to_string()),
    );
    std::fs::create_dir_all(&store_dir)?;

    let store = McStore::open(&descriptor(&store_dir))?;
    // transform 需要 context.db 作为 ContextDomain：生产环境由 host 提供，
    // 这里我们从 upstream 自带的 schema 快照创建并挂载。
    let context_path = store_dir.join("context.db");
    create_test_context_db(&context_path)?;
    store.install_context_domain(Arc::new(SqliteContextDomain::open(&context_path)?));
    let app = Arc::new(App {
        store: Arc::new(store),
        store_dir: store_dir.clone(),
        started_at_ms: now_ms(),
    });

    let router = Router::new()
        .route("/health", get(health))
        .route("/v1/transform", post(do_transform))
        .with_state(app);

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    tracing::info!("mc-adapter listening on http://{addr} (store: {})", store_dir.display());
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, router).await?;
    Ok(())
}
