# Data directories

- `reference/` contains reviewed, versioned inputs required to reproduce a
  workflow, including the World Cup bracket and imported market-price history.
- `runtime/` contains local logs, learned state, databases, paper positions, and generated reports. Git ignores everything in this directory except `.gitkeep`.

Runtime output is intentionally excluded from the repository because it can contain personal trading history, model responses, and environment-specific state.
