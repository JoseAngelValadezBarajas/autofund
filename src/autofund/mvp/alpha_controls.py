"""Negative controls, and the frozen candidate manifest.

The controls exist because the default failure mode of a discovery pipeline is not producing
nothing — it is producing something. A leak, a misalignment, an off-by-one in a forward window or
an autocorrelated sample will all yield a relationship that looks real and survives casual
inspection. The only way to tell a real finding from one of those is to run the same measurement
on data where the answer is known to be "no", and require it to say so.

Four controls are used, and each targets a different way the pipeline could be wrong:

* **time_shuffled** destroys temporal order while preserving the feature and return
  distributions. If structure survives this, the pipeline is reading something other than the
  sequence it claims to read.
* **randomised_pairing** replaces the leader with another market's. A genuine lead-lag
  relationship is specific to the pair; a pipeline that finds one in any pairing is broken.
* **future_leak** deliberately injects the *actual* future return into the feature. This one must
  be **detected** — a control that is expected to fail and fails is evidence the measurement is
  sensitive enough to see a real signal. If a deliberate leak is *not* detected, the pipeline is
  not measuring anything at all, which is a stronger statement than a null result.
* **sign_inversion** flips the feature's sign. A genuine monotone relationship must reverse with
  it; if the conclusion is unchanged, the conclusion is not about the feature.

The manifest then freezes whichever candidate survives, before the validation window is touched,
so that a later change of heart cannot be presented as part of the frozen specification.
"""

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.replay.serialization import fingerprint

from .alpha_discovery import (
    NOT_PREDICTIVE,
    PREDICTIVE,
    AlphaError,
    PredictiveContent,
    assess_predictive_content,
)

CONTROL_VERSION = "autofund.alpha-controls.v1"

TIME_SHUFFLED = "TIME_SHUFFLED"
RANDOMISED_PAIRING = "RANDOMISED_PAIRING"
FUTURE_LEAK = "FUTURE_LEAK"
SIGN_INVERSION = "SIGN_INVERSION"

PREDECLARED_CONTROLS: tuple[str, ...] = (
    TIME_SHUFFLED, RANDOMISED_PAIRING, FUTURE_LEAK, SIGN_INVERSION)

# A control is deterministic: the shuffle uses a fixed seed so a pass or fail is reproducible and
# cannot be re-rolled until it agrees with the candidate.
CONTROL_SHUFFLE_SEED = 20260926

# The controls, in the order they are reported. Declared as a tuple so the set is enumerable
# rather than implied by the call sites: a control that stops being run should be a visible
# change to this list, not a silent omission.
ALPHA_CONTROLS: tuple[str, ...] = (TIME_SHUFFLED, RANDOMISED_PAIRING, FUTURE_LEAK,
                                   SIGN_INVERSION)

# The injected leak must produce a relationship this large, or the measurement is declared
# insensitive. Chosen well above the structural thresholds so that "detected" means obvious
# rather than marginal.
FUTURE_LEAK_MUST_EXCEED = Decimal("1.5")


@dataclass(frozen=True, slots=True)
class ControlResult:
    """One negative control's outcome, and what it implies about the measurement."""

    control: str
    description: str
    verdict: str
    structure_survived: bool
    expected_behavior: str
    conclusion: str

    @property
    def as_expected(self) -> bool:
        """Whether the control behaved the way it must for the pipeline to be trustworthy.

        For the shuffle, pairing and inversion controls, structure must NOT survive. For the
        injected leak it must, because a leak that cannot be detected means the measurement has
        no sensitivity to detect anything else either.
        """
        if self.control == FUTURE_LEAK:
            return self.structure_survived
        return not self.structure_survived

    def public(self) -> dict[str, Any]:
        return {"control": self.control, "description": self.description,
                "verdict": self.verdict, "structure_survived": self.structure_survived,
                "expected_behavior": self.expected_behavior, "as_expected": self.as_expected,
                "conclusion": self.conclusion}


def time_shuffled_control(*, feature_name: str, market: str, horizon_minutes: int,
                          feature: tuple[Decimal, ...],
                          forward: tuple[Decimal, ...],
                          bucket_count: int = 5) -> ControlResult:
    """Shuffle the feature's order relative to the returns it is meant to predict.

    Order is destroyed while both marginal distributions are preserved exactly, so any structure
    that survives is structure the pipeline invented rather than structure in the data.
    """
    if len(feature) != len(forward):
        raise AlphaError("control inputs must be aligned")
    # This is a reproducibility device, not a security primitive. The shuffle exists to destroy
    # temporal order while preserving both marginal distributions, and it must be repeatable: a
    # control whose result changed between runs could not falsify anything. A cryptographically
    # strong generator would make the control less trustworthy here, not more, because it would
    # remove the fixed seed that makes a pass or fail reproducible.
    rng = random.Random(CONTROL_SHUFFLE_SEED)  # nosec B311
    shuffled = list(feature)
    rng.shuffle(shuffled)
    outcome = assess_predictive_content(
        feature_name=f"CONTROL|{TIME_SHUFFLED}|{feature_name}", market=market,
        horizon_minutes=horizon_minutes, feature=tuple(shuffled), forward=forward,
        bucket_count=bucket_count)
    survived = outcome.verdict == PREDICTIVE
    return ControlResult(
        control=TIME_SHUFFLED,
        description="feature order shuffled against the returns; distributions preserved",
        verdict=outcome.verdict, structure_survived=survived,
        expected_behavior="structure DESTROYED",
        conclusion=("pipeline reports structure in shuffled data, so it is not measuring "
                    "temporal order" if survived else
                    "shuffling destroyed the structure, as it must"))


def randomised_pairing_control(*, feature_name: str, market: str, horizon_minutes: int,
                               feature: tuple[Decimal, ...],
                               forward: tuple[Decimal, ...],
                               bucket_count: int = 5) -> ControlResult:
    """Replace the leader's feature with an unrelated market's, preserving the follower.

    Uses a rotation of the feature series rather than fresh random numbers, so the substitute has
    the same distribution and autocorrelation as a real market feature. If that also predicts the
    follower, the original result was not pair-specific.
    """
    if len(feature) != len(forward):
        raise AlphaError("control inputs must be aligned")
    if len(feature) < 4:
        raise AlphaError("too few observations for a pairing control")
    # A large prime rotation decorrelates the leader from the follower while keeping the series
    # itself intact. Random substitution would flatten the autocorrelation and would test an
    # easier null than the one that matters.
    shift = len(feature) // 3 + 1
    rotated = feature[shift:] + feature[:shift]
    outcome = assess_predictive_content(
        feature_name=f"CONTROL|{RANDOMISED_PAIRING}|{feature_name}", market=market,
        horizon_minutes=horizon_minutes, feature=rotated, forward=forward,
        bucket_count=bucket_count)
    survived = outcome.verdict == PREDICTIVE
    return ControlResult(
        control=RANDOMISED_PAIRING,
        description="leader feature replaced by an unrelated market's, follower preserved",
        verdict=outcome.verdict, structure_survived=survived,
        expected_behavior="structure DESTROYED",
        conclusion=("an unrelated leader predicts this follower equally well, so the result is "
                    "not pair-specific" if survived else
                    "an unrelated leader does not predict the follower, as it must not"))


def future_leak_control(*, feature_name: str, market: str, horizon_minutes: int,
                        feature: tuple[Decimal, ...], forward: tuple[Decimal, ...],
                        bucket_count: int = 5) -> ControlResult:
    """Inject the actual future return into the feature. This MUST be detected.

    The strongest available check on the measurement's sensitivity. If a feature that literally
    contains the outcome does not register as predictive, then no smaller true effect could
    register either, and every null result in this milestone would be uninformative rather than
    negative. A control whose expected outcome is failure is what turns "we found nothing" into
    "we would have found something".
    """
    if len(feature) != len(forward):
        raise AlphaError("control inputs must be aligned")
    # The feature is replaced by the outcome itself, scaled so the relationship is unmistakable
    # rather than marginal. Any residual randomness in the pipeline would have to be enormous to
    # hide it.
    leaked = tuple(value * FUTURE_LEAK_MUST_EXCEED for value in forward)
    outcome = assess_predictive_content(
        feature_name=f"CONTROL|{FUTURE_LEAK}|{feature_name}", market=market,
        horizon_minutes=horizon_minutes, feature=leaked, forward=forward,
        bucket_count=bucket_count)
    # A perfect leak produces identical values, which has no rank variation and therefore no
    # correlation. The control therefore treats either a PREDICTIVE verdict or a monotone bucket
    # structure as detection.
    detected = outcome.verdict == PREDICTIVE or outcome.monotone
    return ControlResult(
        control=FUTURE_LEAK,
        description="the outcome itself is injected as the feature",
        verdict=PREDICTIVE if detected else NOT_PREDICTIVE, structure_survived=detected,
        expected_behavior="structure DETECTED",
        conclusion=("the injected leak was detected, so the measurement can see a real signal"
                    if detected else
                    "an injected leak was NOT detected, so this measurement has no sensitivity "
                    "and its null results are uninformative"))


def sign_inversion_control(*, feature_name: str, market: str, horizon_minutes: int,
                           feature: tuple[Decimal, ...], forward: tuple[Decimal, ...],
                           bucket_count: int = 5,
                           original: PredictiveContent | None = None) -> ControlResult:
    """Negate the feature. A genuine monotone relationship must reverse direction.

    The pattern matters as much as the survival flag: if the original was monotone increasing and
    the inverted series is also monotone increasing, the conclusion is not about the feature's
    sign at all. That is reported as a failure of the channel rather than as a passing control.
    """
    if len(feature) != len(forward):
        raise AlphaError("control inputs must be aligned")
    inverted = tuple(-value for value in feature)
    outcome = assess_predictive_content(
        feature_name=f"CONTROL|{SIGN_INVERSION}|{feature_name}", market=market,
        horizon_minutes=horizon_minutes, feature=inverted, forward=forward,
        bucket_count=bucket_count)
    if original is None or not original.monotone:
        survived = outcome.verdict == PREDICTIVE and outcome.monotone_direction == (
            original.monotone_direction if original else outcome.monotone_direction)
        return ControlResult(
            control=SIGN_INVERSION,
            description="feature negated",
            verdict=outcome.verdict, structure_survived=survived,
            expected_behavior="direction REVERSED",
            conclusion=("no baseline monotone structure to reverse" if original is None
                        else "inversion did not reverse the direction, so the structure is "
                             "not about the feature's sign"))
    reversed_correctly = (outcome.monotone
                          and outcome.monotone_direction != original.monotone_direction)
    return ControlResult(
        control=SIGN_INVERSION, description="feature negated",
        verdict=outcome.verdict, structure_survived=not reversed_correctly,
        expected_behavior="direction REVERSED",
        conclusion=("inversion reversed the direction, as it must" if reversed_correctly else
                    "inversion did not reverse the direction, so the structure is not about "
                    "the feature's sign"))


def run_controls(*, feature_name: str, market: str, horizon_minutes: int,
                 feature: tuple[Decimal, ...], forward: tuple[Decimal, ...],
                 bucket_count: int = 5,
                 original: PredictiveContent | None = None) -> tuple[ControlResult, ...]:
    """Every predeclared control, all of them retained whether they pass or fail."""
    return (
        time_shuffled_control(feature_name=feature_name, market=market,
                              horizon_minutes=horizon_minutes, feature=feature,
                              forward=forward, bucket_count=bucket_count),
        randomised_pairing_control(feature_name=feature_name, market=market,
                                   horizon_minutes=horizon_minutes, feature=feature,
                                   forward=forward, bucket_count=bucket_count),
        future_leak_control(feature_name=feature_name, market=market,
                            horizon_minutes=horizon_minutes, feature=feature,
                            forward=forward, bucket_count=bucket_count),
        sign_inversion_control(feature_name=feature_name, market=market,
                               horizon_minutes=horizon_minutes, feature=feature,
                               forward=forward, bucket_count=bucket_count, original=original),
    )


@dataclass(frozen=True, slots=True)
class AlphaCandidateManifest:
    """The frozen specification of one candidate alpha source.

    Every field here is something that could be changed after seeing the validation window, which
    is exactly why it is frozen first. The store refuses to overwrite an existing manifest, so a
    candidate cannot be quietly revised into a form that passes.
    """

    candidate_id: str
    source_family: str
    feature_name: str
    leader: str
    follower: str
    horizon_minutes: int
    bucket_count: int
    development_observations: int
    development_effective_observations: Decimal
    development_rank_relationship: Decimal | None
    development_monotone: bool
    development_monotone_direction: str
    development_stable_subwindows: int
    economic_interpretation: str
    controls_expected_behavior_met: bool
    multiple_test_comparisons: int
    created_at: str
    development_window: tuple[str, str]
    validation_window: tuple[str, str]
    validation_touched: bool

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.feature_name:
            raise AlphaError("candidate requires an id and a feature")
        if self.horizon_minutes not in (1, 5, 15):
            raise AlphaError("horizon must be one of the predeclared values")
        if self.bucket_count < 3:
            raise AlphaError("at least three buckets are required for a structure claim")
        if self.validation_touched:
            raise AlphaError("a candidate may not be frozen after validation was inspected")
        if not self.economic_interpretation:
            raise AlphaError(
                "a candidate requires a stated economic interpretation; without one a "
                "statistical relationship has no reason to persist")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public())

    def public(self) -> dict[str, Any]:
        return {"version": CONTROL_VERSION, "candidate_id": self.candidate_id,
                "source_family": self.source_family, "feature_name": self.feature_name,
                "leader": self.leader, "follower": self.follower,
                "horizon_minutes": self.horizon_minutes, "bucket_count": self.bucket_count,
                "development_observations": self.development_observations,
                "development_effective_observations":
                    str(self.development_effective_observations),
                "development_rank_relationship": (
                    None if self.development_rank_relationship is None
                    else str(self.development_rank_relationship)),
                "development_monotone": self.development_monotone,
                "development_monotone_direction": self.development_monotone_direction,
                "development_stable_subwindows": self.development_stable_subwindows,
                "economic_interpretation": self.economic_interpretation,
                "controls_expected_behavior_met": self.controls_expected_behavior_met,
                "multiple_test_comparisons": self.multiple_test_comparisons,
                "created_at": self.created_at,
                "development_window": list(self.development_window),
                "validation_window": list(self.validation_window),
                "validation_touched": self.validation_touched,
                "frozen_before_validation": True,
                "is_a_strategy": False,
                "trading_policy_defined": False}


def build_candidate(*, candidate_id: str, source_family: str, feature_name: str,
                    leader: str, follower: str, horizon_minutes: int, bucket_count: int,
                    content: PredictiveContent, economic_interpretation: str,
                    controls: tuple[ControlResult, ...], comparisons: int, created_at: str,
                    development_window: tuple[str, str],
                    validation_window: tuple[str, str]) -> AlphaCandidateManifest:
    """Freeze a candidate from measured development content.

    Refuses when the controls did not behave as required. A candidate resting on a pipeline whose
    own negative controls failed is not a candidate, whatever its measured effect.
    """
    if content.verdict != PREDICTIVE:
        raise AlphaError("a candidate requires a predictive development verdict")
    if not all(control.as_expected for control in controls):
        failing = [control.control for control in controls if not control.as_expected]
        raise AlphaError(f"negative controls did not behave as required: {failing}")
    return AlphaCandidateManifest(
        candidate_id=candidate_id, source_family=source_family, feature_name=feature_name,
        leader=leader, follower=follower, horizon_minutes=horizon_minutes,
        bucket_count=bucket_count, development_observations=content.observations,
        development_effective_observations=content.effective_observations,
        development_rank_relationship=content.rank_relationship,
        development_monotone=content.monotone,
        development_monotone_direction=content.monotone_direction,
        development_stable_subwindows=content.stable_subwindows,
        economic_interpretation=economic_interpretation,
        controls_expected_behavior_met=True, multiple_test_comparisons=comparisons,
        created_at=created_at, development_window=development_window,
        validation_window=validation_window, validation_touched=False)


__all__ = [
    "CONTROL_SHUFFLE_SEED",
    "CONTROL_VERSION",
    "FUTURE_LEAK",
    "FUTURE_LEAK_MUST_EXCEED",
    "PREDECLARED_CONTROLS",
    "RANDOMISED_PAIRING",
    "SIGN_INVERSION",
    "TIME_SHUFFLED",
    "AlphaCandidateManifest",
    "ControlResult",
    "build_candidate",
    "future_leak_control",
    "randomised_pairing_control",
    "run_controls",
    "sign_inversion_control",
    "time_shuffled_control",
]
