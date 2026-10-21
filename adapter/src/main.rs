//! mc-adapter: AstrBot 与 Magic Context（ck-mc）之间的路由层。
//!
//! 职责只有一件：把 AstrBot 插件发来的请求，经 subc wire 转发给运行在
//! ck-subc daemon 下的 `magic-context` 模块，再把响应原样返回。
//!
//! 这里不做任何策略判断（不决定丢什么、不降档、不压缩），策略全部在 ck-mc 内部。

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
use serde_json::{json, Value};
use subc_client_rs::{CallOptions, ConsumerOptions, SubcConsumer};
use subc_protocol::{BindIdentity, RouteTarget};

mod historian;

/// daemon 里注册的模块 id（见 ~/.config/cortexkit/subc.jsonc 的 modules 键）
const MODULE_ID: &str = "magic-context";
/// 我们作为 host 的身份名
const HARNESS: &str = "astrbot";

struct App {
    consumer: Arc<SubcConsumer>,
    project_root: PathBuf,
    call_timeout: std::time::Duration,
    started_at_ms: u128,
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

async fn health(State(app): State<Arc<App>>) -> impl IntoResponse {
    Json(json!({
        "ok": true,
        "service": "mc-adapter",
        "version": env!("CARGO_PKG_VERSION"),
        "mode": "subc",
        "module_id": MODULE_ID,
        "harness": HARNESS,
        "project_root": app.project_root.to_string_lossy(),
        "uptime_ms": now_ms().saturating_sub(app.started_at_ms),
    }))
}

/// 把请求体原样转发给 ck-mc，响应原样返回。
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

    // 会话身份：mc 用它区分记忆/历史空间
    let session = raw
        .get("session_id")
        .and_then(|v| v.as_str())
        .unwrap_or("astrbot-default")
        .to_string();

    let identity = BindIdentity::new(app.project_root.clone(), HARNESS, session.clone());
    let target = RouteTarget::ToolProvider {
        module_id: MODULE_ID.to_string(),
    };

    let payload = serde_json::to_vec(&raw).map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "status": "error", "error": format!("encode: {e}") })),
        )
    })?;

    let opts = CallOptions {
        timeout: app.call_timeout,
        ..Default::default()
    };

    match app
        .consumer
        .call(target, identity, payload, opts)
        .await
    {
        Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
            Ok(v) => Ok(Json(v)),
            Err(e) => Err((
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "status": "error",
                    "error": format!("module returned non-JSON: {e}"),
                    "session_id": session,
                })),
            )),
        },
        Err(e) => Err((
            StatusCode::BAD_GATEWAY,
            Json(json!({
                "status": "error",
                "error": format!("subc call failed: {e}"),
                "session_id": session,
            })),
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

    // mc 以 project 为单位组织记忆；AstrBot 场景用固定根，保证跨会话一致
    let project_root = PathBuf::from(
        std::env::var("MC_PROJECT_ROOT").unwrap_or_else(|_| "/root/data/mc-adapter".to_string()),
    );

    let consumer = SubcConsumer::connect_default(ConsumerOptions::default()).await?;
    tracing::info!(
        "mc-adapter connected to subc daemon (module={MODULE_ID}, harness={HARNESS})"
    );

    let app = Arc::new(App {
        consumer: Arc::new(consumer),
        project_root: project_root.clone(),
        call_timeout: std::time::Duration::from_secs(
            std::env::var("MC_CALL_TIMEOUT_SECS")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(60),
        ),
        started_at_ms: now_ms(),
    });

    // historian 认领循环：默认关闭。
    // 发布版 ck-mc-alpha.22464bf2 尚未开放 historian.pending/claim/complete
    // （实测返回 unrecognized_request_shape），开启只会空转刷错误日志。
    // 上游开放后设置 MC_HISTORIAN_ENABLED=1 + MC_LLM_* 即可启用。
    let historian_enabled = std::env::var("MC_HISTORIAN_ENABLED")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    if historian_enabled {
        match historian::llm_config_from_env() {
            Some(llm) => {
                tracing::info!("historian worker enabled (model={}, every 20s)", llm.model);
                historian::spawn(
                    app.consumer.clone(),
                    project_root.clone(),
                    llm,
                    std::time::Duration::from_secs(20),
                );
            }
            None => tracing::warn!(
                "MC_HISTORIAN_ENABLED set but MC_LLM_BASE_URL / MC_LLM_MODEL missing"
            ),
        }
    } else {
        tracing::info!(
            "historian worker disabled (release ck-mc is slice-1: transform/status only; set MC_HISTORIAN_ENABLED=1 to try)"
        );
    }

    let router = Router::new()
        .route("/health", get(health))
        .route("/v1/transform", post(do_transform))
        .with_state(app);

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    tracing::info!("mc-adapter listening on http://{addr}");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, router).await?;
    Ok(())
}
