# evals

Scoring code and frozen expectations for labelled tenants. No customer data, no raw values, no tenant identifiers beyond what the scoring needs.

Each agent adds `evals/<agent>/` with: the scoring script, the metric definitions, and a frozen baseline to check regressions against. Run the baseline check before changing an agent's rules or tools.
