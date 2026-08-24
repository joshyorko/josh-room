# ADR 0006 — Camp selective salvage

Camp is prior art, not a Josh Room dependency. Port only archive traversal
rejection, ownership-aware staging cleanup, immutable object/read-back digest
checks, non-empty destination protection, and atomic promotion as small tests.
Reject Camp's DevPod/Devsy adapters, forwarding, T3 hosting, provider
activation, leases, generations, supervision, and concurrent-writer state
machine.
