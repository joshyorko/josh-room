# Deferred integrations

Josh Room currently consumes the immutable JAT RCC v18.19.2 Environment Artifact
for bootstrap and asks JAT for `rcc_environment=auto` during Save. RCC/JAT own
artifact production, acquisition, and materialization; Josh Room carries only
optional typed receipt metadata in the encrypted inner manifest.

Actions Runtime integration, Hive adapter/projections, OpenAI uploads,
archive-provider selection, and model-facing projections remain deferred.
