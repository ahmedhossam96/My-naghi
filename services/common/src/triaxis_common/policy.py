"""Data-handling policy engine and enforcement (ADR 0020).

A :class:`HandlingPolicy` (versioned YAML in ``config/policies/``) maps a
classification result to a decision per destination:

- level rules: what each sensitivity level may do at each destination;
- tag rules: stricter handling for specific data types (e.g. credentials are
  blocked before inference, national IDs are redacted);
- workload rules: which workloads may receive data of a given level;
- brand overrides: stricter (or explicitly different) rules per brand.

The strictest applicable decision wins (BLOCK > RESTRICT > REDACT > ALLOW).
A missing policy, an unknown tenant or any engine error fails closed: the
caller receives ``classification_unavailable`` and nothing is processed.

Reclassification is not possible through a request. Clients can only raise
the level (see :class:`~triaxis_common.classification.ClassificationContext`);
lowering requires a reviewed policy change in Git.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from triaxis_common.classification import (
    ClassificationContext,
    ClassificationResult,
    Classified,
    Classifier,
    redact,
)
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.logging import AllowListLogger, Event, Level
from triaxis_common.taxonomy import (
    Classification,
    Control,
    DataTag,
    Decision,
    Destination,
    ReasonCode,
)
from triaxis_common.types import Slug, Workload

_DECISION_REASON: Final = {
    Decision.ALLOW: ReasonCode.POLICY_ALLOW,
    Decision.REDACT: ReasonCode.POLICY_REDACT,
    Decision.RESTRICT: ReasonCode.POLICY_RESTRICT,
    Decision.BLOCK: ReasonCode.POLICY_BLOCK,
}

VersionStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]{1,3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrandOverride(_Strict):
    levels: dict[Destination, dict[Classification, Decision]] = {}
    tags: dict[Destination, dict[DataTag, Decision]] = {}
    workloads: dict[Classification, frozenset[Workload]] = {}


class HandlingPolicy(_Strict):
    """A versioned data-handling policy. One file per policy in ``config/policies``."""

    schema_version: int = Field(1, ge=1, le=1)
    policy_id: Slug
    version: VersionStr
    owner: Annotated[str, Field(min_length=1, max_length=80)]
    baseline: dict[Workload, Classification]
    levels: dict[Destination, dict[Classification, Decision]]
    tags: dict[Destination, dict[DataTag, Decision]] = {}
    workloads: dict[Classification, frozenset[Workload]] = {}
    controls: dict[Classification, tuple[Control, ...]] = {}
    brands: dict[Slug, BrandOverride] = {}

    @model_validator(mode="after")
    def _complete(self) -> Self:
        missing_workloads = set(Workload) - self.baseline.keys()
        if missing_workloads:
            raise ValueError(f"baseline missing for workloads: {sorted(missing_workloads)}")
        for destination in Destination:
            rules = self.levels.get(destination)
            if rules is None or set(Classification) - rules.keys():
                raise ValueError(f"levels.{destination.value} must define every level")
        return self

    @property
    def version_id(self) -> str:
        return f"{self.policy_id}@{self.version}"


def load_policy(path: Path) -> HandlingPolicy:
    with path.open("rb") as fh:
        return HandlingPolicy.model_validate(yaml.safe_load(fh))


def load_policies(directory: Path) -> dict[str, HandlingPolicy]:
    policies: dict[str, HandlingPolicy] = {}
    for path in sorted(directory.glob("*.yaml")):
        policy = load_policy(path)
        if policy.policy_id != path.stem:
            raise ValueError(f"{path.name}: policy_id must match the file name")
        policies[policy.policy_id] = policy
    return policies


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    redact_tags: frozenset[DataTag]
    required_controls: tuple[Control, ...]
    reason_codes: tuple[ReasonCode, ...]
    policy_version: str


@dataclass(frozen=True, slots=True)
class Enforced:
    """What a caller may pass on. ``text`` is None when blocked."""

    text: str | None
    result: ClassificationResult

    @property
    def blocked(self) -> bool:
        return self.text is None


def _strictest(*decisions: Decision) -> Decision:
    return max(decisions, key=lambda d: d.rank)


class PolicyEngine:
    """Evaluates classification results against a tenant's handling policy."""

    def __init__(
        self,
        policies: Mapping[str, HandlingPolicy],
        tenant_policy: Mapping[str, str],
        classifier: Classifier | None = None,
        logger: AllowListLogger | None = None,
    ) -> None:
        self._policies = dict(policies)
        self._tenant_policy = dict(tenant_policy)  # tenant id -> policy id
        self._classifier = classifier or Classifier()
        self._logger = logger

    def policy_for(self, tenant: str) -> HandlingPolicy:
        policy_id = self._tenant_policy.get(tenant)
        policy = self._policies.get(policy_id) if policy_id else None
        if policy is None:
            raise TriaxisError(ErrorCode.CLASSIFICATION_UNAVAILABLE)
        return policy

    def context(
        self,
        tenant: str,
        brand: str,
        workload: Workload,
        *,
        correlation_id: str | None = None,
        requested_level: Classification | None = None,
        source: ClassificationResult | None = None,
        declared_tags: frozenset[DataTag] = frozenset(),
    ) -> ClassificationContext:
        policy = self.policy_for(tenant)
        return ClassificationContext(
            tenant=tenant,
            brand=brand,
            workload=workload,
            correlation_id=correlation_id,
            baseline=policy.baseline[workload],
            requested_level=requested_level,
            source_level=source.level if source else None,
            source_tags=(frozenset(source.data_tags) if source else frozenset()) | declared_tags,
        )

    def decide(self, result: ClassificationResult, destination: Destination) -> PolicyDecision:
        policy = self.policy_for(result.tenant)
        brand = policy.brands.get(result.brand, BrandOverride())
        reasons: list[ReasonCode] = []

        level_rules = {**policy.levels[destination], **brand.levels.get(destination, {})}
        decision = level_rules[result.level]

        tag_rules = {**policy.tags.get(destination, {}), **brand.tags.get(destination, {})}
        redact_tags: set[DataTag] = set()
        for tag in result.data_tags:
            tag_decision = tag_rules.get(tag)
            if tag_decision is None:
                continue
            decision = _strictest(decision, tag_decision)
            if tag_decision is Decision.REDACT:
                redact_tags.add(tag)
        if decision is Decision.REDACT and not redact_tags:
            redact_tags = set(result.data_tags)  # level-wide redaction: every detected type

        allowed = {**policy.workloads, **brand.workloads}.get(result.level)
        if allowed is not None and result.workload not in allowed:
            decision = Decision.BLOCK
            reasons.append(ReasonCode.WORKLOAD_NOT_PERMITTED)

        reasons.insert(0, _DECISION_REASON[decision])
        controls = list(policy.controls.get(result.level, ()))
        if decision is Decision.REDACT:
            controls.append(Control.REDACT_BEFORE_INFERENCE)
        return PolicyDecision(
            decision=decision,
            redact_tags=frozenset(redact_tags),
            required_controls=tuple(dict.fromkeys(controls)),
            reason_codes=tuple(reasons),
            policy_version=policy.version_id,
        )

    def classify(self, text: str, ctx: ClassificationContext) -> Classified:
        return self._classifier.classify(text, ctx)

    def enforce(
        self,
        text: str,
        ctx: ClassificationContext,
        destination: Destination,
        *,
        audit: bool = True,
    ) -> Enforced:
        """Classify, decide and apply the decision. Fails closed on any error."""
        try:
            classified = self._classifier.classify(text, ctx)
            decision = self.decide(classified.result, destination)
        except TriaxisError:
            self._audit_failure(ctx)
            raise
        except (ValidationError, KeyError, ValueError, TypeError):
            self._audit_failure(ctx)
            raise TriaxisError(ErrorCode.CLASSIFICATION_UNAVAILABLE) from None
        result = classified.result.model_copy(
            update={
                "decision": decision.decision,
                "required_controls": decision.required_controls,
                "reason_codes": classified.result.reason_codes + decision.reason_codes,
                "policy_version": decision.policy_version,
            }
        )
        if audit:
            self._audit(result, destination)
        if decision.decision is Decision.BLOCK:
            return Enforced(None, result)
        if decision.decision is Decision.REDACT or decision.redact_tags:
            return Enforced(
                redact(classified.text, classified.detections, decision.redact_tags), result
            )
        return Enforced(classified.text, result)

    def audit(self, result: ClassificationResult) -> None:
        """Record a metadata-only decision event (e.g. an aggregated output result)."""
        self._audit(result, Destination.CLIENT_RESPONSE)

    def _audit(self, result: ClassificationResult, destination: Destination) -> None:
        if self._logger is None:
            return
        del destination  # destinations are not on the log allow-list; decision suffices
        self._logger.log(
            Event.CLASSIFICATION_DECISION,
            Level.INFO,
            classification=result.level,
            decision=result.decision,
            data_tags=list(result.data_tags),
            reason_codes=list(result.reason_codes),
            policy_version=result.policy_version,
            tenant=result.tenant,
            brand=result.brand,
            workload=result.workload,
        )

    def _audit_failure(self, ctx: ClassificationContext) -> None:
        if self._logger is not None:
            self._logger.log(
                Event.CLASSIFICATION_DECISION,
                Level.WARNING,
                decision=Decision.BLOCK,
                reason_codes=[ReasonCode.CLASSIFIER_ERROR],
                error_code=ErrorCode.CLASSIFICATION_UNAVAILABLE,
                tenant=ctx.tenant,
                brand=ctx.brand,
                workload=ctx.workload,
            )


def summarise(result: ClassificationResult) -> dict[str, Any]:
    """Client/UI view of a result: labels and codes only."""
    return {
        "level": result.level.value,
        "decision": result.decision.value,
        "data_tags": [t.value for t in result.data_tags],
        "privacy_tags": [t.value for t in result.privacy_tags],
        "required_controls": [c.value for c in result.required_controls],
        "reason_codes": [r.value for r in result.reason_codes],
        "policy_version": result.policy_version,
        "classifier_version": result.classifier_version,
    }


def merge_results(results: list[ClassificationResult]) -> ClassificationResult:
    """Aggregate several results (e.g. chat messages, output segments) into one view."""
    if not results:
        raise ValueError("nothing to merge")
    worst = max(results, key=lambda r: (r.level.rank, r.decision.rank))
    decision = max((r.decision for r in results), key=lambda d: d.rank)
    counts: dict[DataTag, int] = {}
    for r in results:
        for tag, n in r.tag_counts.items():
            counts[tag] = counts.get(tag, 0) + n
    return worst.model_copy(
        update={
            "decision": decision,
            "data_tags": tuple(sorted({t for r in results for t in r.data_tags})),
            "privacy_tags": tuple(sorted({p for r in results for p in r.privacy_tags})),
            "tag_counts": dict(sorted(counts.items())),
            "required_controls": tuple(
                dict.fromkeys(c for r in results for c in r.required_controls)
            ),
            "reason_codes": tuple(dict.fromkeys(c for r in results for c in r.reason_codes)),
        }
    )
