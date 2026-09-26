# Third-party references

Projects whose **ideas** informed `agentkit` (see
[0003](../decisions/0003-clean-room-and-dependencies.md)). No code, prompts,
tool descriptions or fixtures were copied from any of them; none is a runtime
dependency unless marked.

| Project | License | Idea taken |
|---|---|---|
| PydanticAI (github.com/pydantic/pydantic-ai) | MIT | one native model class per provider on the vendor SDK; `provider:model` refs; fallback model; test/function models for offline runs |
| genai-prices (github.com/pydantic/genai-prices) | MIT | a price table keyed by model, unknown model means unknown cost |
| mini-swe-agent (github.com/SWE-agent/mini-swe-agent) | MIT | tiny loop with a linear, append-only history; limits raised as exceptions; every command an independent subprocess |
| SWE-agent (github.com/SWE-agent/SWE-agent) | MIT | agent-computer interface: purpose-built tools with guardrails and concise output |
| OpenHands Software Agent SDK (github.com/OpenHands/software-agent-sdk) | MIT | event-sourced conversation with resume; stuck detector; secret redaction; risk-rated tools |
| OpenAI Codex CLI (github.com/openai/codex) | Apache-2.0 | sandboxed exec with a scrubbed environment; network off by default; read-only network mode |
| Anthropic sandbox-runtime (github.com/anthropics/sandbox-runtime) | Apache-2.0 | filesystem and network allowlists enforced outside the model; domain rules with subdomain matching |
| OpenAI Agents SDK (github.com/openai/openai-agents-python) | MIT | runner/agent split; guardrail tripwires; human approval of tool calls |
| LangGraph (github.com/langchain-ai/langgraph) | MIT | checkpoint after every step; interrupt and resume |
| Letta (github.com/letta-ai/letta) | Apache-2.0 | bounded, persisted state for long-running agents |
| goose (github.com/aaif-goose/goose) | Apache-2.0 | recipe files as shareable playbooks (informs `agent.yaml` + `playbook/`) |
| inspect_ai (github.com/UKGovernmentBEIS/inspect_ai) | MIT | task = dataset + solver + scorer; model-graded rubric scoring; eval logs |
| Aider (github.com/Aider-AI/aider) | Apache-2.0 | exact search/replace edits that work across models |
| smolagents (github.com/huggingface/smolagents) | Apache-2.0 | small readable loop; pluggable executors |
| CrewAI (github.com/crewAIInc/crewAI) | MIT | an explicit expected output per task (our milestone deliverables) |
| LiteLLM (github.com/BerriAI/litellm) | MIT (enterprise dir excluded) | a single canonical request shape; **avoided as a dependency** |
