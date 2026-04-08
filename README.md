# 🔧 Grooming Agent

AI-powered backlog grooming agent built with LangGraph + Claude. Connects to Jira and Linear, processes each task through a full grooming cycle, syncs results back to both tools, and stops when it needs a human.

## What it does

For each task in your backlog the agent runs five steps automatically:

1. **Enrich description** — clarifies vague tasks using context from other backlog items. Stops and asks if the description is too empty to work with.
2. **Estimate** — scores tasks using Fibonacci (1, 2, 3, 5, 8, 13) with a confidence level (high/medium/low). Stops and asks if confidence is low.
3. **Split if large** — automatically breaks tasks over 8 SP into subtasks of 5 SP or less.
4. **Acceptance criteria** — writes Given/When/Then scenarios and Definition of Done.
5. **Prioritize** — scores each task using RICE framework and assigns P0/P1/P2/P3.

After each task is processed, the agent automatically:
- Updates the task in **Jira** — enriched description and acceptance criteria
- Creates or updates the task in **Linear** — with story points, priority, description, and acceptance criteria

## How it works

```
Load tasks from Jira + Linear
        ↓
  Pick next task
        ↓
  Enrich → Estimate → Split → Acceptance criteria → Prioritize
        ↓                ↓
   human checkpoint  human checkpoint
   (if unclear)      (if low confidence)
        ↓
  Sync to Jira + Linear
        ↓
  Next task → Finalize
```

## Quick start

```bash
# 1. Clone
git clone https://github.com/artemsanisimow-ux/grooming-agent.git
cd grooming-agent

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Dependencies
pip install langgraph langchain-anthropic langgraph-checkpoint-sqlite python-dotenv requests

# 4. Create .env
touch .env
```

Add to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
JIRA_URL=yourcompany.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_API_TOKEN=...
LINEAR_API_KEY=lin_api_...
LINEAR_PROJECT_SLUG=
LANGUAGE=en
```

```bash
# 5. Run
python3 grooming_agent.py
```

The agent will show your Jira and Linear projects — pick which one to groom.

If no credentials are set it runs on built-in demo tasks.

## Language support

Fully bilingual — all terminal output, prompts to the model, and reports follow the selected language.

```bash
python3 grooming_agent.py --lang en
python3 grooming_agent.py --lang ru
```

Priority: `--lang` flag → `LANGUAGE` in `.env` → default `ru`

## Output

Two files saved after each session:

- `grooming_report_SESSION_TIMESTAMP.md` — all tasks grouped by priority (P0 → P3) with story points, descriptions, acceptance criteria, and subtasks
- `grooming_audit_SESSION_TIMESTAMP.json` — full log of every step with timestamps

## Resume a session

```python
run_grooming(
    session_id="20260402_154425",
    resume=True
)
```

## Files

| File | Description |
|------|-------------|
| `grooming_agent.py` | Main agent — LangGraph graph, nodes, state |
| `jira_sync.py` | Writes enriched tasks back to Jira |
| `linear_sync.py` | Creates/updates tasks in Linear |
| `i18n.py` | Bilingual string library |

## Built with

- [LangGraph](https://github.com/langchain-ai/langgraph) — graph-based agent orchestration
- [Claude](https://anthropic.com) — language model (claude-opus-4-5)
- SQLite — checkpointing between sessions
- Jira REST API — task source and sync target
- Linear GraphQL API — task source and sync target

## Part of a larger system

| Agent | Repo | Description |
|-------|------|-------------|
| Discovery | [discovery-agent](https://github.com/artemsanisimow-ux/discovery-agent) | Raw data → insights → hypotheses |
| Grooming | this repo | Jira + Linear → estimate → acceptance criteria → prioritize → sync |
| Planning | coming soon | Groomed tasks → sprint plan with Monte Carlo + pre-mortem |
