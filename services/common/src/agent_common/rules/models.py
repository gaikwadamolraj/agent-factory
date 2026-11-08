"""The rubric document: how a set of graded criteria becomes a score, a band and a recommendation.

This is the `agent-rules` capability's data model (docs/impl_multi_domain.md section 2.2). It is deliberately
declarative and closed: a rubric chooses from a fixed vocabulary of components, deductions and caps, and
cannot express arbitrary logic. That is a security property, not a style preference - a rubric is pack data,
authored outside the code review that protects the platform, so it must not be able to compute anything the
engine did not already agree to compute.

Three rules shaped this model:

* **No model ever computes a score.** Everything here is arithmetic over grades the engine already holds
  (CLAUDE.md section 2 forbids LLM arithmetic). An LLM may produce a *grade*; the points are ours.
* **The vocabulary belongs to the domain, not to Python.** Grades, component names, band labels and
  recommendation words are all rubric data, so insurance says "covered / excluded / conditional /
  insufficient_evidence" and banking says "mandatory control met" without touching this file.
* **Selection is by attribute, never by meaning.** A component selects criteria by matching attributes the
  caller supplies (`importance`, `core`, `clause_type`, ...). The engine has no idea what "must" means, which
  is exactly why the same engine serves every domain.
"""
from typing import Literal, Optional, Union

from pydantic import Field, field_validator, model_validator

from agent_common.models import ApiModel

# A key names something inside the rubric (a component, a cap, an input). Kept to the same shape as pack keys
# so a rubric reads like the rest of the pack.
KEY = r"^[a-z][a-z0-9_]{0,63}$"

# Bounds that make a malicious or careless rubric harmless: the engine refuses one that cannot produce a
# bounded score instead of trusting it at run time.
MAX_POINTS = 1000.0
MAX_ITEMS = 50


class GradeSpec(ApiModel):
    """One grade in the domain's vocabulary and what it is worth, from 0 (no credit) to 1 (full credit)."""

    weight: float = Field(ge=0.0, le=1.0)
    label: str = Field(default="", max_length=64)


class Selector(ApiModel):
    """Which criteria a component, cap or deduction applies to.

    Each entry is an attribute name and the values that match. An empty selector matches every criterion.
    Values are compared as lower-case strings, and booleans as `true`/`false`, so a rubric says
    `{"importance": ["must"], "core": ["true"]}` without the engine knowing what either means.
    """

    attributes: dict[str, list[str]] = Field(default_factory=dict, max_length=10)

    @field_validator("attributes")
    @classmethod
    def _non_empty_values(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, allowed in value.items():
            if not allowed:
                raise ValueError(f"selector '{name}' lists no values")
            if len(allowed) > MAX_ITEMS:
                raise ValueError(f"selector '{name}' lists too many values")
        return value

    @property
    def matches_everything(self) -> bool:
        return not self.attributes


class GradedComponent(ApiModel):
    """Points earned from the grades of the criteria this component selects.

    `points` is the component's full weight; the score is `points * mean(grade weight of each selected
    criterion)`. With nothing selected the component falls back - to another component's selection
    (`fallback_component`) or to every criterion (`fallback_to_all`) - which is what keeps a rubric
    meaningful when a source names only optional criteria.
    """

    kind: Literal["graded"] = "graded"
    key: str = Field(pattern=KEY)
    points: float = Field(ge=0.0, le=MAX_POINTS)
    select: Selector = Field(default_factory=Selector)
    fallback_component: Optional[str] = Field(default=None, pattern=KEY)
    fallback_to_all: bool = False


class InputComponent(ApiModel):
    """Points taken from a number the caller supplies, clamped into the component's range.

    The caller names the input; the rubric decides what it is worth. `thresholds` turn a ratio into points
    (first threshold whose `min` the value reaches wins); without them the value *is* the points, clamped.
    `fallback_input` is used when the primary input is absent, which is how "years of experience, or the
    model's seniority judgement when the source states no minimum" stays a rubric decision.
    """

    kind: Literal["input"] = "input"
    key: str = Field(pattern=KEY)
    points: float = Field(ge=0.0, le=MAX_POINTS)
    input: str = Field(pattern=KEY)
    thresholds: list["Threshold"] = Field(default_factory=list, max_length=MAX_ITEMS)
    fallback_input: Optional[str] = Field(default=None, pattern=KEY)

    @model_validator(mode="after")
    def _thresholds_descend(self) -> "InputComponent":
        """Thresholds are read in order, so an unordered list would silently award the wrong points."""
        minimums = [threshold.min for threshold in self.thresholds]
        if minimums != sorted(minimums, reverse=True):
            raise ValueError(f"component '{self.key}': thresholds must be ordered from highest min to lowest")
        for threshold in self.thresholds:
            if threshold.points > self.points:
                raise ValueError(f"component '{self.key}': a threshold awards more than the component's points")
        return self


class Threshold(ApiModel):
    """`min` is the lowest input value that earns `points`."""

    min: float
    points: float = Field(ge=0.0, le=MAX_POINTS)


Component = Union[GradedComponent, InputComponent]


class Deduction(ApiModel):
    """Points removed when a condition holds. Exactly one condition kind per deduction.

    * `flag` - the caller reported a named boolean (an integrity flag, a sanctions hit).
    * `majority_grade` - more than half of the criteria graded `among` carry `grade`. This is how "mostly
      asserted rather than evidenced" costs something, in any domain.
    """

    key: str = Field(pattern=KEY)
    points: float = Field(ge=0.0, le=MAX_POINTS)
    flag: Optional[str] = Field(default=None, pattern=KEY)
    majority_grade: Optional[str] = None
    among: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    select: Selector = Field(default_factory=Selector)

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> "Deduction":
        conditions = [self.flag is not None, self.majority_grade is not None]
        if sum(conditions) != 1:
            raise ValueError(f"deduction '{self.key}': set exactly one of 'flag' or 'majorityGrade'")
        if self.majority_grade is not None and not self.among:
            raise ValueError(f"deduction '{self.key}': 'majorityGrade' needs 'among' to compare against")
        return self


class Cap(ApiModel):
    """An upper bound on the final score when too much of what matters is missing.

    Caps are read in order and the **first** that applies wins, so a rubric states its decisive condition
    first. This is where "any exclusion is decisive" lives for insurance, and "half the must-haves are
    absent" for recruitment - the same mechanism, different data.

    `min_fraction` is the share of the selected criteria that must carry one of `grades` (1 criterion out of
    1 with `min_fraction: 0` means "any"). `reason` is shown to a person, and may contain `{names}`, which is
    replaced with the criteria that triggered it.
    """

    key: str = Field(pattern=KEY)
    max_score: float = Field(ge=0.0, le=MAX_POINTS)
    grades: list[str] = Field(min_length=1, max_length=MAX_ITEMS)
    min_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    select: Selector = Field(default_factory=Selector)
    reason: str = Field(default="", max_length=200)


class Band(ApiModel):
    """A named range of the final score, with the recommendation that goes with it.

    `min` is inclusive and bands are read from the highest down, so the first match wins. The recommendation
    lives here rather than in a second table because a band and its advice always change together.
    """

    min: float = Field(ge=0.0, le=MAX_POINTS)
    band: str = Field(min_length=1, max_length=64)
    recommendation: str = Field(default="", max_length=128)


class RubricDocument(ApiModel):
    """One complete, deterministic scoring rubric: the whole input to `agent_common.rules.score`.

    Validated on publish like the rest of a pack, so an unusable rubric is refused at authoring time rather
    than failing on a user's request.
    """

    key: str = Field(pattern=KEY)
    version: str = Field(default="1", max_length=32)
    display_name: str = Field(default="", max_length=128)
    max_score: float = Field(default=100.0, gt=0.0, le=MAX_POINTS)
    grades: dict[str, GradeSpec] = Field(min_length=1, max_length=MAX_ITEMS)
    components: list[Component] = Field(default_factory=list, max_length=MAX_ITEMS)
    deductions: list[Deduction] = Field(default_factory=list, max_length=MAX_ITEMS)
    caps: list[Cap] = Field(default_factory=list, max_length=MAX_ITEMS)
    bands: list[Band] = Field(min_length=1, max_length=MAX_ITEMS)
    # Grades that count as "the criterion was met" when a caller asks for a summary. Names only; the engine
    # uses it for reporting, never for the score.
    positive_grades: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    # What to tell a person when nothing could be graded at all. The score is then 0 whatever the components
    # would have awarded: a subject nobody could assess has not passed, and saying so is the honest outcome.
    empty_criteria_reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _coherent(self) -> "RubricDocument":
        """Every name a rubric uses must exist, and the points must be able to reach the maximum.

        These are the mistakes that would otherwise surface as a quietly wrong score: a cap naming a grade
        the vocabulary does not have never fires, and components that cannot reach `max_score` make every
        band unreachable.
        """
        keys = [component.key for component in self.components]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate component key")
        if len({cap.key for cap in self.caps}) != len(self.caps):
            raise ValueError("duplicate cap key")
        if len({deduction.key for deduction in self.deductions}) != len(self.deductions):
            raise ValueError("duplicate deduction key")

        known_grades = set(self.grades)
        for cap in self.caps:
            unknown = sorted(set(cap.grades) - known_grades)
            if unknown:
                raise ValueError(f"cap '{cap.key}' names grades this rubric does not define: {', '.join(unknown)}")
            if cap.max_score > self.max_score:
                raise ValueError(f"cap '{cap.key}' caps above the rubric's maximum score")
        for deduction in self.deductions:
            unknown = sorted(set(deduction.among) - known_grades)
            if deduction.majority_grade is not None and deduction.majority_grade not in known_grades:
                raise ValueError(f"deduction '{deduction.key}' names an unknown grade")
            if unknown:
                raise ValueError(f"deduction '{deduction.key}' compares against unknown grades")
        unknown_positive = sorted(set(self.positive_grades) - known_grades)
        if unknown_positive:
            raise ValueError(f"positiveGrades names unknown grades: {', '.join(unknown_positive)}")

        for component in self.components:
            if isinstance(component, GradedComponent) and component.fallback_component:
                if component.fallback_component not in keys:
                    raise ValueError(f"component '{component.key}' falls back to an unknown component")
                if component.fallback_component == component.key:
                    raise ValueError(f"component '{component.key}' falls back to itself")

        total = sum(component.points for component in self.components)
        if self.components and round(total, 6) != round(self.max_score, 6):
            raise ValueError(f"components award {total:g} points but the rubric's maximum is {self.max_score:g}")

        minimums = [band.min for band in self.bands]
        if minimums != sorted(minimums, reverse=True):
            raise ValueError("bands must be ordered from the highest min to the lowest")
        if self.bands[-1].min > 0:
            raise ValueError("the lowest band must start at 0, or a score below it would have no band")
        return self

    def grade_weight(self, grade: str) -> float:
        """The weight of a grade, or 0 for one this rubric does not define.

        Unknown grades score nothing rather than raising: a model that invents a grade must not be able to
        fail a run, and must certainly not be able to earn credit for a word nobody agreed to.
        """
        spec = self.grades.get(grade)
        return spec.weight if spec is not None else 0.0

    def grade_label(self, grade: str) -> str:
        spec = self.grades.get(grade)
        return (spec.label or grade) if spec is not None else grade


InputComponent.model_rebuild()
