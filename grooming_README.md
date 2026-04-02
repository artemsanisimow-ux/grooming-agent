# 🔧 Grooming Agent

AI-powered backlog grooming agent built with LangGraph + Claude. Connects to Jira and Linear, processes each task through a full grooming cycle, and stops when it needs a human.

## What it does

For each task in your backlog the agent runs five steps automatically:

1. **Enrich description** — clarifies vague tasks using context from other backlog items. Stops and asks if the description is too empty to work with.
2. **Estimate** — scores tasks using Fibonacci (1, 2, 3, 5, 8, 13) with a confidence level (high / medium / low). Stops and asks if confidence is low.
3. **Split if large** — automatically breaks tasks over 8 SP into subtasks of 5 SP or less.
4. **Acceptance criteria** — writes Given/When/Then scenarios and Definition of Done.
5. **Prioritize** — scores each task using the RICE framework and assigns P0 / P1 / P2 / P3.

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
  Save groomed task → next task
        ↓
     Finalize
```

State is persisted in SQLite — you can close the terminal and resume the session later.
Every step is logged with timestamps in an audit JSON file.

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

Add your credentials to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
JIRA_URL=yourcompany.atlassian.net
JIRA_EMAIL=your@email.com
JIRA_API_TOKEN=...
LINEAR_API_KEY=lin_api_...
LINEAR_PROJECT_SLUG=
```

```bash
# 5. Run
python3 grooming_agent.py
```

The agent will show available Jira and Linear projects — pick the one you want to groom.

If no credentials are set it runs on built-in demo tasks so you can try it immediately.

## Output

Two files are saved after each session:

- `grooming_report_SESSION_TIMESTAMP.md` — all tasks grouped by priority (P0 → P3) with story points, descriptions, acceptance criteria, and subtasks
- `grooming_audit_SESSION_TIMESTAMP.json` — full step-by-step log with timestamps and what went into context

## Resume a session

```python
run_grooming(
    session_id="20260402_154425",
    resume=True
)
```

## Built with

- [LangGraph](https://github.com/langchain-ai/langgraph) — graph-based agent orchestration and state persistence
- [Claude](https://anthropic.com) — language model (claude-opus-4-5)
- SQLite — checkpointing between sessions
- Jira REST API — task source
- Linear GraphQL API — task source

## Part of a larger system

| Agent | Repo | Description |
|-------|------|-------------|
| Discovery | [discovery-agent](https://github.com/artemsanisimow-ux/discovery-agent) | Research → insights → backlog → hypotheses |
| Grooming | this repo | Jira + Linear → estimate → acceptance criteria → prioritize |
| Planning | coming soon | Groomed tasks → sprint plan based on velocity and capacity |
