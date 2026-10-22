//! Historian host worker.
//!
//! ck-mc 自己不会跑 LLM：它把「要压缩哪段历史」做成 pending run，
//! 由 host 按 `historian.pending → claim → (跑模型) → complete` 的协议认领并回填。
//! 本模块就是这个 host 端的循环，完全按上游协议走，不自造 prompt。

use std::{sync::Arc, time::Duration};

use serde_json::{json, Value};
use subc_client_rs::{CallOptions, SubcConsumer};
use subc_protocol::{BindIdentity, RouteTarget};

const MODULE_ID: &str = "magic-context";
const HARNESS: &str = "astrbot";
const CLAIMANT: &str = "astrbot-adapter";

#[derive(Clone, Debug)]
pub struct LlmConfig {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub timeout_secs: u64,
}

/// 从插件写下的 llm.json 读取三个角色的模型配置。
/// 文件不存在或解析失败时返回 None，调用方回退到环境变量。
pub fn load_from_file() -> Option<LlmConfig> {
    let path = std::env::var("MC_LLM_CONFIG")
        .unwrap_or_else(|_| "/root/data/mc-adapter/data/llm.json".to_string());
    let text = std::fs::read_to_string(&path).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    let role = v.get("historian").or_else(|| v.get("dreamer"))?;
    let base_url = role.get("base_url").and_then(|x| x.as_str())?.to_string();
    let model = role.get("model").and_then(|x| x.as_str())?.to_string();
    if base_url.is_empty() || model.is_empty() {
        return None;
    }
    let api_key = role
        .get("api_key")
        .and_then(|x| x.as_str())
        .unwrap_or_default()
        .to_string();
    let timeout_secs = std::env::var("MC_LLM_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(120);
    Some(LlmConfig {
        base_url,
        api_key,
        model,
        timeout_secs,
    })
}

pub fn llm_config_from_env() -> Option<LlmConfig> {
    let base_url = std::env::var("MC_LLM_BASE_URL").ok()?;
    let api_key = std::env::var("MC_LLM_API_KEY").unwrap_or_default();
    let model = std::env::var("MC_LLM_MODEL").ok()?;
    let timeout_secs = std::env::var("MC_LLM_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(120);
    Some(LlmConfig {
        base_url,
        api_key,
        model,
        timeout_secs,
    })
}

/// 后台 historian 循环。会话由 daemon 侧按 project 归属，这里只负责把活干完。
pub fn spawn(
    consumer: Arc<SubcConsumer>,
    project_root: std::path::PathBuf,
    llm: LlmConfig,
    interval: Duration,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(llm.timeout_secs))
            .build();

        let client = match client {
            Ok(c) => c,
            Err(e) => {
                tracing::error!("historian: failed to build http client: {e}");
                return;
            }
        };

        loop {
            // 每轮重新读插件写下的配置：在 AstrBot 面板里换模型无需重启服务
            let current = load_from_file().unwrap_or_else(|| llm.clone());
            if let Err(e) = tick(&consumer, &project_root, &current, &client).await {
                tracing::warn!("historian tick failed: {e}");
            }
            tokio::time::sleep(interval).await;
        }
    })
}

async fn call_module(
    consumer: &SubcConsumer,
    project_root: &std::path::Path,
    body: Value,
) -> Result<Value, String> {
    let identity = BindIdentity::new(project_root.to_path_buf(), HARNESS, "historian");
    let target = RouteTarget::ToolProvider {
        module_id: MODULE_ID.to_string(),
    };
    let bytes = serde_json::to_vec(&body).map_err(|e| e.to_string())?;
    let resp = consumer
        .call(target, identity, bytes, CallOptions::default())
        .await
        .map_err(|e| format!("subc call: {e}"))?;
    serde_json::from_slice::<Value>(&resp).map_err(|e| format!("decode: {e}"))
}

async fn tick(
    consumer: &SubcConsumer,
    project_root: &std::path::Path,
    llm: &LlmConfig,
    client: &reqwest::Client,
) -> Result<(), String> {
    // 1. 问 daemon 有没有待办的压缩任务
    let pending = call_module(
        consumer,
        project_root,
        json!({ "method": "historian.pending", "v": 1 }),
    )
    .await?;

    let runs = pending
        .get("runs")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    if runs.is_empty() {
        return Ok(());
    }

    for run in runs {
        let run_id = match run.get("run_id").and_then(Value::as_str) {
            Some(v) => v.to_string(),
            None => continue,
        };

        // 2. 认领：拿回 mc 生成的 system/user prompt
        let claim = call_module(
            consumer,
            project_root,
            json!({
                "method": "historian.claim",
                "v": 1,
                "run_id": run_id,
                "claimant_instance_id": CLAIMANT,
            }),
        )
        .await?;

        if claim.get("ok").and_then(Value::as_bool) != Some(true) {
            tracing::debug!("historian: claim refused for {run_id}");
            continue;
        }

        let token = claim
            .get("token")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let system = claim
            .pointer("/prompt/system")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let user = claim
            .pointer("/prompt/user")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();

        // 3. 跑模型（host 的职责）
        let output = match run_llm(llm, client, &system, &user).await {
            Ok(text) => text,
            Err(e) => {
                tracing::warn!("historian: llm failed for {run_id}: {e}");
                // 回报失败，让任务回队列而不是卡死
                let _ = call_module(
                    consumer,
                    project_root,
                    json!({
                        "method": "historian.complete",
                        "v": 1,
                        "run_id": run_id,
                        "token": token,
                        "error": { "message": e },
                    }),
                )
                .await;
                continue;
            }
        };

        // 4. 回传
        let done = call_module(
            consumer,
            project_root,
            json!({
                "method": "historian.complete",
                "v": 1,
                "run_id": run_id,
                "token": token,
                "output": { "text": output },
            }),
        )
        .await?;

        tracing::info!(
            "historian: completed {run_id} ok={}",
            done.get("ok").and_then(|v| v.as_bool()).unwrap_or(false)
        );
    }

    Ok(())
}

async fn run_llm(
    llm: &LlmConfig,
    client: &reqwest::Client,
    system: &str,
    user: &str,
) -> Result<String, String> {
    let body = json!({
        "model": llm.model,
        "messages": [
            { "role": "system", "content": system },
            { "role": "user", "content": user }
        ],
        "stream": false,
    });

    let url = format!("{}/chat/completions", llm.base_url.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);
    if !llm.api_key.is_empty() {
        req = req.bearer_auth(&llm.api_key);
    }

    let resp = req.send().await.map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("upstream {status}: {}", &text[..text.len().min(300)]));
    }

    let v: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    v.pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .map(|s| s.to_string())
        .ok_or_else(|| "no choices[0].message.content".to_string())
}
