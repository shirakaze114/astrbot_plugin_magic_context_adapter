# Personal conversation model fallback policy

- Use only the primary provider selected for the personal conversation.
- `agent_runner.config.model.fallback_provider_ids` must be `[]`.
- If the primary provider fails, surface the error; do not switch providers.
- This is an AstrBot runner configuration policy, not a Magic Context routing rule.

## Local application

On 2026-10-22 the personal profile configuration was backed up and its fallback
provider list was cleared. The primary provider was left unchanged.
The credential-bearing configuration and its backup remain local and are not
included in this repository. Runtime reload/application is not verified by this
commit; an already running request may still hold its previous configuration.
