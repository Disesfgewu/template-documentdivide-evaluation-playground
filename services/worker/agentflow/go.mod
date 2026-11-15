// Isolated module: the worker's agentKit integration (issue #6). Builds only
// in an authorized checkout with the private agentKit submodule initialized
// (docs/AGENTKIT_SUBMODULE.md); public CI never compiles it.
// Run: scripts/check.sh --scope worker-agentkit
module github.com/Disesfgewu/template-documentdivide-evaluation-playground/services/worker/agentflow

go 1.27.1

require github.com/Disesfgewu/agentkit v0.0.0-00010101000000-000000000000

replace github.com/Disesfgewu/agentkit => ../../../third_party/agentkit
