# Prompt 06 — Performance Optimisation

Look at the 3 most expensive aggregation queries from our sample set.

For each one:
1. Use EXPLAIN to show how ClickHouse is currently executing it
2. Propose the best optimisation strategy and explain your reasoning
3. Implement it and show the before/after query and performance

Also review the full query set for any that are doing full table scans when they
shouldn't be. If you find any, explain what the ORDER BY key design would need to
look like to fix them.
