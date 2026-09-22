import importlib
import inspect
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from josh_room.adapter_contract import (
    AdapterError,
    AdapterErrorCode,
    BoundedRecordStream,
    CancellationToken,
    Checkpoint,
    Decision,
    GateDecision,
    GateSet,
    LogicalSourceName,
    ProbeRequest,
    ResolveResult,
    ResolveStatus,
    SourceEvent,
    SourceRecord,
    StreamLimits,
)
from josh_room.synthetic_adapter import (
    BuiltinAdapterRegistry,
    SyntheticAdapter,
    SyntheticSource,
    builtin_registry,
)

GOLDENS = Path(__file__).parent / "goldens"


class AllowPolicy:
    def evaluate(self, _event, _declaration):
        return GateDecision(Decision.ALLOW, ("synthetic-policy-allow",), "policy-authority")


class AllowMaterial:
    def evaluate(self, _event, _declaration):
        return GateDecision(Decision.ALLOW, ("synthetic-material-allow",), "material-authority")


class DenyPolicy:
    def evaluate(self, _event, _declaration):
        return GateDecision(Decision.DENY, ("caller-policy-deny",), "policy-authority")


class SecretRaisingPolicy:
    def evaluate(self, _event, _declaration):
        raise RuntimeError("/private/credentials/token https://evil.invalid bearer-secret")


class SecretRaisingMaterial:
    def evaluate(self, _event, _declaration):
        raise RuntimeError("/private/material.key https://evil.invalid material-secret")


def _gates():
    return GateSet(policy=AllowPolicy(), material=AllowMaterial())


def _event(session_id="session-one"):
    return SourceEvent(
        event_id="event-one",
        logical_source=LogicalSourceName.TRANSCRIPT,
        session_id=session_id,
        approved_root="synthetic-root",
    )


def _adapter(*, sources=None, max_record_bytes=8, max_open_bytes=64):
    return SyntheticAdapter(
        sources or (
            SyntheticSource(
                logical_source=LogicalSourceName.TRANSCRIPT,
                session_id="session-one",
                source_id="source-one",
                representation="active-jsonl",
                records=(b"alpha", b"beta"),
            ),
        ),
        max_record_bytes=max_record_bytes,
        max_open_bytes=max_open_bytes,
    )


def test_builtin_registry_is_closed_and_exposes_stable_logical_names():
    registry = builtin_registry()

    assert registry.logical_sources() == (
        "codex.session-metadata",
        "codex.transcript",
    )
    assert registry.get("codex.transcript") is not None
    assert registry.get("codex.session-metadata") is not None
    with pytest.raises(AdapterError) as error:
        registry.get("repo-provided.adapter")
    assert error.value.code is AdapterErrorCode.UNKNOWN_SOURCE

    with pytest.raises(AdapterError) as error:
        BuiltinAdapterRegistry({
            "codex.transcript": registry.get("codex.transcript"),
            "codex.session-metadata": registry.get("codex.session-metadata"),
            "repo-provided.adapter": registry.get("codex.transcript"),
        })
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST

    with pytest.raises(TypeError):
        vars(registry)
    assert not hasattr(registry, "_adapters")
    for name in ("_adapters", "_BuiltinAdapterRegistry__adapter_map", "arbitrary_state"):
        with pytest.raises(AttributeError):
            setattr(registry, name, {})
        with pytest.raises(AttributeError):
            object.__setattr__(registry, name, {})
    assert registry.logical_sources() == (
        "codex.session-metadata",
        "codex.transcript",
    )


def test_probe_and_inspect_are_public_safe_and_stable():
    adapter = _adapter()

    probe = adapter.probe(ProbeRequest(LogicalSourceName.TRANSCRIPT, "session-one"))
    inspect = adapter.inspect()

    assert json.loads(probe.to_json()) == {
        "capabilities": ["checkpoint", "open", "resolve"],
        "logical_source": "codex.transcript",
        "representations": ["active-jsonl"],
        "session_id": "session-one",
        "source_exists": True,
    }
    assert json.loads(inspect.to_json())["adapter"] == {"name": "synthetic", "version": "1"}
    assert "/" not in inspect.to_json()


def test_plan_is_content_free_and_matches_golden():
    plan = _adapter().plan(_event(), _gates())

    assert json.loads(plan.to_json()) == json.loads((GOLDENS / "adapter_plan.json").read_text())
    assert "alpha" not in plan.to_json()
    assert "beta" not in plan.to_json()
    assert "destination" not in plan.to_json()
    assert "recipient" not in plan.to_json()
    assert "profile" not in plan.to_json()


def test_checkpoint_and_complete_result_have_stable_json_forms():
    adapter = _adapter()
    plan = adapter.plan(_event(), _gates())
    stream = adapter.open(plan)
    list(stream)

    checkpoint = stream.result.next_checkpoint
    assert json.loads(checkpoint.to_json()) == json.loads((GOLDENS / "adapter_checkpoint.json").read_text())
    assert json.loads(stream.result.to_json()) == json.loads((GOLDENS / "adapter_result.json").read_text())
    assert adapter.checkpoint(stream.result) == checkpoint
    assert adapter.checkpoint(stream.result) == checkpoint


@pytest.mark.parametrize(("field", "value", "code"), [
    ("next_byte_offset", 0, AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT),
    ("observed_size", 8, AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT),
    ("prefix_digest", "0" * 64, AdapterErrorCode.SOURCE_PREFIX_CHANGED),
    ("source_id", "forged-source", AdapterErrorCode.SOURCE_REPLACED),
    ("representation", "future-format", AdapterErrorCode.UNKNOWN_REPRESENTATION),
])
def test_checkpoint_publication_and_resume_reject_forged_cursors(field, value, code):
    adapter = _adapter()
    stream = adapter.open(adapter.plan(_event(), _gates()))
    next(stream)
    saved = stream.result.next_checkpoint
    forged = replace(saved, **{field: value})
    forged_result = replace(stream.result, next_checkpoint=forged)

    with pytest.raises(AdapterError) as publication_error:
        adapter.checkpoint(forged_result)
    assert publication_error.value.code is code

    with pytest.raises(AdapterError) as resume_error:
        adapter.plan(_event(), _gates(), prior_checkpoint=forged)
    assert resume_error.value.code is code
    assert "alpha" not in str(resume_error.value)
    assert "/" not in str(resume_error.value)


def test_resume_is_idempotent_and_does_not_repeat_acknowledged_records():
    adapter = _adapter()
    first = adapter.open(adapter.plan(_event(), _gates()))
    next(first)
    first_result = first.result
    saved = adapter.checkpoint(first_result)

    resumed_plan = adapter.plan(_event(), _gates(), prior_checkpoint=saved)
    resumed = adapter.open(resumed_plan)

    assert list(resumed) == [SourceRecord(
        logical_source=LogicalSourceName.TRANSCRIPT,
        session_id="session-one",
        record_index=1,
        content=b"beta",
        material_class="transcript",
    )]


@pytest.mark.parametrize("mutation,code", [
    ("prefix", AdapterErrorCode.SOURCE_PREFIX_CHANGED),
    ("truncate", AdapterErrorCode.SOURCE_TRUNCATED),
    ("replace", AdapterErrorCode.SOURCE_REPLACED),
])
def test_resume_rejects_unstable_source_ranges(mutation, code):
    adapter = _adapter()
    first = adapter.open(adapter.plan(_event(), _gates()))
    next(first)
    saved = adapter.checkpoint(first.result)
    adapter.mutate(mutation)

    with pytest.raises(AdapterError) as error:
        adapter.plan(_event(), _gates(), prior_checkpoint=saved)
    assert error.value.code is code
    assert "alpha" not in str(error.value)
    assert "/" not in str(error.value)


def test_representation_transition_is_resolved_without_resetting_cursor():
    active = SyntheticSource(
        LogicalSourceName.TRANSCRIPT, "session-one", "source-one", "active-jsonl", (b"alpha", b"beta")
    )
    archived = replace(active, representation="archived-jsonl")
    adapter = _adapter(sources=(active, archived))
    first = adapter.open(adapter.plan(_event(), _gates()))
    next(first)
    saved = adapter.checkpoint(first.result)
    adapter.select_representation("session-one", "archived-jsonl")

    published = adapter.checkpoint(first.result)
    assert published.representation == "archived-jsonl"
    assert published.next_record_index == saved.next_record_index
    assert published.next_byte_offset == saved.next_byte_offset
    assert published.observed_size == saved.observed_size
    assert published.prefix_digest == saved.prefix_digest

    resolved = adapter.resolve("session-one", saved)
    resumed = adapter.open(adapter.plan(_event(), _gates(), prior_checkpoint=saved))

    assert resolved.status.value == "moved"
    assert resolved.representation == "archived-jsonl"
    assert [record.content for record in resumed] == [b"beta"]


def test_known_but_unapproved_representation_transition_conflicts():
    active = SyntheticSource(
        LogicalSourceName.TRANSCRIPT, "session-one", "source-one", "active-jsonl", (b"alpha", b"beta")
    )
    compressed = replace(active, representation="compressed-jsonl-zst")
    adapter = _adapter(sources=(active, compressed))
    first = adapter.open(adapter.plan(_event(), _gates()))
    next(first)
    saved = adapter.checkpoint(first.result)
    adapter.select_representation("session-one", "compressed-jsonl-zst")

    with pytest.raises(AdapterError) as error:
        adapter.plan(_event(), _gates(), prior_checkpoint=saved)
    assert error.value.code is AdapterErrorCode.SOURCE_REPRESENTATION_CHANGED
    assert adapter.resolve("session-one", saved).status is ResolveStatus.CONFLICT


def test_unknown_representation_is_quarantined():
    source = SyntheticSource(
        LogicalSourceName.TRANSCRIPT, "session-one", "source-one", "future-format", (b"alpha",)
    )
    adapter = _adapter(sources=(source,))

    result = adapter.resolve("session-one", None)
    plan = adapter.plan(_event(), _gates())

    assert result.status.value == "quarantine"
    assert plan.status == "quarantine"
    assert result.to_json() == '{"representation":"future-format","session_id":"session-one","status":"quarantine"}'


def test_unknown_prior_representation_is_quarantined_and_cannot_be_planned():
    adapter = _adapter()
    stream = adapter.open(adapter.plan(_event(), _gates()))
    next(stream)
    unknown = replace(stream.result.next_checkpoint, representation="future-format")

    result = adapter.resolve("session-one", unknown)

    assert result.status is ResolveStatus.QUARANTINE
    with pytest.raises(AdapterError) as error:
        adapter.plan(_event(), _gates(), prior_checkpoint=unknown)
    assert error.value.code is AdapterErrorCode.UNKNOWN_REPRESENTATION


def test_resolve_and_selection_cover_registered_metadata_source():
    adapter = _adapter(sources=(SyntheticSource(
        LogicalSourceName.SESSION_METADATA,
        "metadata-session",
        "metadata-source",
        "active-jsonl",
        (b"metadata",),
    ),))

    resolved = adapter.resolve(
        "metadata-session",
        None,
        logical_source=LogicalSourceName.SESSION_METADATA,
    )
    adapter.select_representation(
        "metadata-session",
        "active-jsonl",
        logical_source=LogicalSourceName.SESSION_METADATA,
    )

    assert resolved.status is ResolveStatus.FOUND
    assert adapter.probe(ProbeRequest(
        LogicalSourceName.SESSION_METADATA,
        "metadata-session",
    )).source_exists


def test_plan_does_not_read_synthetic_records():
    class NoReadRecords(tuple):
        def __iter__(self):
            raise AssertionError("plan read source records")

        def __getitem__(self, _index):
            raise AssertionError("plan indexed source records")

    source = SyntheticSource(
        LogicalSourceName.TRANSCRIPT,
        "session-one",
        "source-one",
        "active-jsonl",
        (b"alpha", b"beta"),
    )
    object.__setattr__(source, "records", NoReadRecords((b"alpha", b"beta")))
    adapter = _adapter(sources=(source,))

    plan = adapter.plan(_event(), _gates())

    assert plan.status == "ready"
    assert plan.estimated_records == 2
    assert plan.estimated_bytes == 9


def test_stream_is_bounded_pull_based_and_cancellable():
    adapter = _adapter(max_open_bytes=8)
    plan = adapter.plan(_event(), _gates())
    stream = adapter.open(plan)

    assert next(stream).content == b"alpha"
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.STREAM_LIMIT

    token = CancellationToken()
    cancelled = adapter.open(plan, cancellation=token)
    token.cancel()
    with pytest.raises(AdapterError) as error:
        next(cancelled)
    assert error.value.code is AdapterErrorCode.CANCELLED


def test_oversized_record_is_rejected_without_raw_content_in_error():
    adapter = _adapter(
        sources=(SyntheticSource(
            LogicalSourceName.TRANSCRIPT, "session-one", "source-one", "active-jsonl", (b"123456789",)
        ),),
        max_record_bytes=8,
    )
    stream = adapter.open(adapter.plan(_event(), _gates()))

    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.RECORD_OVERSIZE
    assert "123456789" not in str(error.value)


def test_asset_record_uses_asset_bound_not_only_record_bound():
    checkpoint = Checkpoint(
        logical_source=LogicalSourceName.TRANSCRIPT,
        session_id="session-one",
        source_id="source-one",
        representation="active-jsonl",
        next_record_index=0,
        next_byte_offset=0,
        observed_size=0,
        prefix_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    stream = BoundedRecordStream(
        (SourceRecord(
            logical_source=LogicalSourceName.TRANSCRIPT,
            session_id="session-one",
            record_index=0,
            content=b"asset-content",
            material_class="asset",
        ),),
        lambda _count, _bytes: checkpoint,
        StreamLimits(max_record_bytes=64, max_asset_bytes=4, max_open_records=8, max_open_bytes=64),
    )

    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.RECORD_OVERSIZE
    assert "asset-content" not in str(error.value)


def test_policy_and_material_gates_are_mandatory_and_adapter_cannot_select_authority():
    adapter = _adapter()
    with pytest.raises(AdapterError) as error:
        adapter.plan(_event(), None)
    assert error.value.code is AdapterErrorCode.GATES_REQUIRED

    plan = adapter.plan(_event(), _gates())
    assert plan.policy.decision is Decision.ALLOW
    assert plan.material.decision is Decision.ALLOW
    assert not hasattr(plan, "destination")
    assert not hasattr(plan, "recipients")
    assert not hasattr(plan, "retention")


def test_open_rejects_forged_plan_status_limits_and_gate_decisions():
    adapter = _adapter(max_open_bytes=8)
    ready = adapter.plan(_event(), _gates())

    inflated_limits = replace(
        ready.limits,
        max_open_bytes=1024,
        max_record_bytes=1024,
    )
    forged_limits = replace(ready, limits=inflated_limits)
    forged_gates = replace(
        ready,
        policy=GateDecision(Decision.DENY, ("forged-policy",), "forged-authority"),
    )

    blocked = adapter.plan(
        _event(),
        GateSet(
            policy=DenyPolicy(),
            material=AllowMaterial(),
        ),
    )
    forged_status = replace(blocked, status="ready")

    for forged in (forged_limits, forged_gates, forged_status):
        with pytest.raises(AdapterError) as error:
            adapter.open(forged)
        assert error.value.code is AdapterErrorCode.INVALID_REQUEST


def test_plan_binding_is_private_and_forged_plan_cannot_capture():
    adapter = _adapter()
    plan = adapter.plan(_event(), _gates())
    module = sys.modules[SyntheticAdapter.__module__]

    assert not hasattr(adapter, "_plan_salt")
    assert not hasattr(adapter, "_plan_integrity")
    with pytest.raises(TypeError):
        vars(adapter)
    assert all(
        marker not in name.lower()
        for name in dir(adapter)
        for marker in ("salt", "integrity", "binding", "seal", "authority")
    )
    assert "salt" not in repr(adapter).lower()
    assert "_authority" not in repr(plan)
    assert "integrity" not in plan.to_json()
    module_helpers = {
        name: value
        for name, value in vars(module).items()
        if callable(value)
    }
    assert not any(
        marker in name.lower()
        for name in module_helpers
        for marker in ("seal", "binding", "salt", "integrity")
    )
    assert not any(
        inspect.isfunction(helper) and helper.__closure__
        for helper in module_helpers.values()
    )

    forged = replace(
        plan,
        limits=replace(plan.limits, max_open_bytes=1024),
        status="ready",
        policy=GateDecision(Decision.ALLOW, ("forged-policy",), "forged-authority"),
    )
    with pytest.raises(AdapterError) as error:
        adapter.open(forged)
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST

    with pytest.raises(AdapterError) as error:
        _adapter().open(plan)
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST


def test_plan_issuance_has_no_replaceable_adapter_slot():
    adapter = _adapter()
    blocked_adapter = _adapter()
    blocked = blocked_adapter.plan(
        _event(),
        GateSet(policy=DenyPolicy(), material=AllowMaterial()),
    )

    assert all("plan_records" not in name for name in SyntheticAdapter.__slots__)
    assert not hasattr(adapter, "_SyntheticAdapter__plan_records")
    for name in ("_SyntheticAdapter__plan_records", "_plan_records", "_issued_plans"):
        with pytest.raises(AttributeError):
            object.__setattr__(blocked_adapter, name, ((blocked, ()),))

    object.__setattr__(blocked, "status", "ready")
    object.__setattr__(blocked.policy, "decision", Decision.ALLOW)
    with pytest.raises(AdapterError) as error:
        blocked_adapter.open(blocked)
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST


def test_open_revalidates_mutated_source_records_before_emitting():
    source = SyntheticSource(
        LogicalSourceName.TRANSCRIPT,
        "session-one",
        "source-one",
        "active-jsonl",
        (b"alpha", b"beta"),
    )
    adapter = _adapter(sources=(source,))
    plan = adapter.plan(_event(), _gates())
    object.__setattr__(source, "records", (b"forged", b"beta"))

    with pytest.raises(AdapterError) as error:
        adapter.open(plan)
    assert error.value.code is AdapterErrorCode.SOURCE_PREFIX_CHANGED
    assert "forged" not in str(error.value)

    source = SyntheticSource(
        LogicalSourceName.TRANSCRIPT,
        "session-one",
        "source-one",
        "active-jsonl",
        (b"alpha", b"beta"),
    )
    adapter = _adapter(sources=(source,))
    stream = adapter.open(adapter.plan(_event(), _gates()))
    object.__setattr__(source, "records", (b"forged", b"beta"))
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.SOURCE_PREFIX_CHANGED
    assert "forged" not in str(error.value)


def test_stream_limits_are_slotted_and_old_limits_slot_cannot_bypass_bounds():
    adapter = _adapter(max_open_bytes=8)
    stream = adapter.open(adapter.plan(_event(), _gates()))

    with pytest.raises(AttributeError):
        stream.__dict__  # noqa: B018 - adversarial introspection test
    for name in ("_max_record_bytes", "_max_asset_bytes", "_max_open_records", "_max_open_bytes"):
        with pytest.raises(AttributeError):
            setattr(stream, name, 1024)
    for name in ("_limits", "_bounds", "arbitrary_state"):
        with pytest.raises(AttributeError):
            setattr(stream, name, StreamLimits(1024, 1024, 1024, 1024))
        with pytest.raises(AttributeError):
            object.__setattr__(stream, name, StreamLimits(1024, 1024, 1024, 1024))

    assert next(stream).content == b"alpha"
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.STREAM_LIMIT


def test_stream_rejects_forged_records_and_post_init_state_rebinding():
    adapter = _adapter()
    stream = adapter.open(adapter.plan(_event(), _gates()))

    with pytest.raises(AttributeError):
        stream._records = iter((object(),))
    for name in (
        "_limits",
        "_record_count",
        "_byte_count",
        "_done",
        "_status",
        "_cancellation",
        "_checkpoint_factory",
        "_result",
        "arbitrary_state",
    ):
        with pytest.raises(AttributeError):
            setattr(stream, name, object())

    assert next(stream).content == b"alpha"


def test_stream_facade_rejects_low_level_slot_injection_and_keeps_bounds():
    adapter = _adapter(max_open_bytes=8)
    stream = adapter.open(adapter.plan(_event(), _gates()))
    forged = SourceRecord(
        logical_source=LogicalSourceName.TRANSCRIPT,
        session_id="session-one",
        record_index=0,
        content=b"forged",
        material_class="transcript",
    )
    attempts = (
        ("_records", iter((forged,))),
        ("_limits", StreamLimits(1024, 1024, 1024, 1024)),
        ("_max_open_bytes", 1024),
        ("_record_count", 0),
        ("_byte_count", 0),
        ("_status", object()),
        ("_cancellation", CancellationToken()),
        ("_checkpoint_factory", lambda _count, _bytes: stream.result.next_checkpoint),
        ("arbitrary_state", object()),
    )
    assert BoundedRecordStream.__slots__ == ()
    with pytest.raises(AttributeError):
        stream.__dict__  # noqa: B018 - adversarial introspection test
    for name, value in attempts:
        with pytest.raises(AttributeError):
            setattr(stream, name, value)
        with pytest.raises(AttributeError):
            object.__setattr__(stream, name, value)

    assert next(stream).content == b"alpha"
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.STREAM_LIMIT


def test_stream_registry_rebinding_cannot_replace_private_state(monkeypatch):
    adapter = _adapter(max_open_bytes=8)
    stream = adapter.open(adapter.plan(_event(), _gates()))
    state_module = importlib.import_module("josh_room._stream_state")
    contract_module = importlib.import_module("josh_room.adapter_contract")

    assert not hasattr(state_module, "_registry")
    monkeypatch.setattr(state_module, "_registry", object(), raising=False)
    monkeypatch.setattr(contract_module, "_STREAM_BOUNDS", {id(stream): (1024,)}, raising=False)

    assert next(stream).content == b"alpha"
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.STREAM_LIMIT


def test_slotted_values_reject_mutation_and_issued_stream_stays_bounded():
    adapter = _adapter(max_open_bytes=8)
    plan = adapter.plan(_event(), _gates())
    values = (
        adapter,
        adapter.declaration,
        plan,
        plan.limits,
        plan.policy,
        plan.checkpoint,
        adapter.probe(ProbeRequest(LogicalSourceName.TRANSCRIPT, "session-one")),
        adapter.inspect(),
        adapter.resolve("session-one", None),
    )
    for value in values:
        with pytest.raises(AttributeError):
            value.__dict__  # noqa: B018 - adversarial introspection test
    assert not hasattr(plan, "_authority")
    assert not hasattr(adapter, "_issued_plans")
    assert not hasattr(adapter, "_declaration_limits")
    for name in ("declaration", "_sources"):
        with pytest.raises(AttributeError):
            setattr(adapter, name, ())
    for name in ("_issued_plans", "_declaration_limits", "arbitrary_state"):
        with pytest.raises(AttributeError):
            setattr(adapter, name, ())
        with pytest.raises(AttributeError):
            object.__setattr__(adapter, name, ())
    with pytest.raises(AttributeError):
        setattr(plan, "status", "ready")  # noqa: B010 - adversarial mutation test

    blocked_adapter = _adapter()
    blocked = blocked_adapter.plan(
        _event(),
        GateSet(policy=DenyPolicy(), material=AllowMaterial()),
    )
    object.__setattr__(blocked, "status", "ready")
    object.__setattr__(blocked.policy, "decision", Decision.ALLOW)
    object.__setattr__(blocked.limits, "max_open_bytes", 1024)
    with pytest.raises(AdapterError) as error:
        blocked_adapter.open(blocked)
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST

    declaration_adapter = _adapter()
    declaration_plan = declaration_adapter.plan(_event(), _gates())
    object.__setattr__(declaration_adapter.declaration.limits, "max_open_bytes", 1024)
    with pytest.raises(AdapterError) as error:
        declaration_adapter.open(declaration_plan)
    assert error.value.code is AdapterErrorCode.INVALID_REQUEST

    stream = adapter.open(plan)
    object.__setattr__(plan.limits, "max_open_bytes", 1024)
    next(stream)
    with pytest.raises(AdapterError) as error:
        next(stream)
    assert error.value.code is AdapterErrorCode.STREAM_LIMIT


@pytest.mark.parametrize(
    ("policy", "material"),
    (
        (SecretRaisingPolicy(), AllowMaterial()),
        (AllowPolicy(), SecretRaisingMaterial()),
    ),
)
def test_gate_exceptions_are_opaque_typed_errors(policy, material):
    with pytest.raises(AdapterError) as error:
        _adapter().plan(_event(), GateSet(policy=policy, material=material))

    assert error.value.code is AdapterErrorCode.INVALID_RESULT
    assert str(error.value) == "invalid-result: invalid result"
    assert error.value.__context__ is None
    assert "/private/" not in str(error.value)
    assert "evil.invalid" not in str(error.value)
    assert "secret" not in str(error.value)


def test_malicious_result_cannot_inject_path_url_command_or_profile():
    adapter = _adapter()
    with pytest.raises(AdapterError) as error:
        adapter.checkpoint({
            "next_checkpoint": {
                "path": "/private/secret",
                "url": "https://evil.invalid",
                "command": "rm -rf /",
                "profile": "personal",
            }
        })
    assert error.value.code is AdapterErrorCode.INVALID_RESULT
    assert "/private/secret" not in str(error.value)
    assert "evil.invalid" not in str(error.value)
    assert "rm -rf" not in str(error.value)


def test_public_error_and_result_forms_reject_unsafe_details():
    with pytest.raises(AdapterError) as error:
        raise AdapterError(AdapterErrorCode.INVALID_RESULT, "/private/secret https://evil.invalid")
    assert "/private/secret" not in str(error.value)
    assert "evil.invalid" not in str(error.value)

    with pytest.raises(AdapterError) as error:
        ResolveResult(ResolveStatus.FOUND, "session-one", "https://evil.invalid")
    assert "evil.invalid" not in str(error.value)

    inspection = _adapter().inspect()
    with pytest.raises(TypeError):
        inspection.adapter["path"] = "/private/secret"
    assert "/private/secret" not in inspection.to_json()


def test_selection_state_rejects_unregistered_or_unknown_sources():
    adapter = _adapter()

    with pytest.raises(AdapterError) as error:
        adapter.select_representation("session-one", "future-format")
    assert error.value.code is AdapterErrorCode.UNKNOWN_REPRESENTATION

    with pytest.raises(AdapterError) as error:
        adapter.select_representation("missing-session", "active-jsonl")
    assert error.value.code is AdapterErrorCode.UNKNOWN_SOURCE


def test_two_planners_and_checkpoint_writers_are_deterministic():
    adapter = _adapter()
    plans = []
    barrier = threading.Barrier(2)

    def plan_once():
        barrier.wait()
        plans.append(adapter.plan(_event(), _gates()))

    threads = [threading.Thread(target=plan_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [plan.to_json() for plan in plans] == [plans[0].to_json(), plans[0].to_json()]
    results = [adapter.open(plans[0]).result]
    results.append(adapter.open(plans[1]).result)
    checkpoints = [adapter.checkpoint(result) for result in results]
    assert checkpoints[0].to_json() == checkpoints[1].to_json()


def test_checkpoint_constructor_rejects_absolute_paths_and_unknown_fields():
    with pytest.raises(AdapterError) as error:
        Checkpoint.from_json(json.dumps({
            "logical_source": "codex.transcript",
            "session_id": "session-one",
            "source_id": "source-one",
            "representation": "active-jsonl",
            "next_record_index": 0,
            "next_byte_offset": 0,
            "observed_size": 0,
            "prefix_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "exported_path": "/tmp/secret",
        }))
    assert error.value.code is AdapterErrorCode.INVALID_CHECKPOINT
