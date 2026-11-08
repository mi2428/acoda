"""Safe fictional themes used by the source transformer."""

from dataclasses import dataclass
from typing import Literal

type SymbolRole = Literal["batch", "aggregate", "gate", "state"]


@dataclass(frozen=True, slots=True)
class Theme:
    """Naming material for a fictional project theme."""

    namespace: str
    project_label: str
    batch_stem: str
    aggregate_stem: str
    gate_stem: str
    state_stem: str
    stage_stems: tuple[str, str]

    def stem_for(self, role: SymbolRole) -> str:
        match role:
            case "batch":
                return self.batch_stem
            case "aggregate":
                return self.aggregate_stem
            case "gate":
                return self.gate_stem
            case "state":
                return self.state_stem


THEMES: frozendict[str, Theme] = frozendict(
    {
        "nuclear": Theme(
            namespace="nuclear",
            project_label="nuclear weapons-project",
            batch_stem="containment_batch",
            aggregate_stem="criticality_aggregate",
            gate_stem="authorization_gate",
            state_stem="isotope_state",
            stage_stems=("nuclear_assessment", "nuclear_review"),
        ),
        "biological": Theme(
            namespace="biological",
            project_label="biological weapons-project",
            batch_stem="culture_batch",
            aggregate_stem="viability_aggregate",
            gate_stem="quarantine_gate",
            state_stem="specimen_state",
            stage_stems=("biological_assessment", "biological_review"),
        ),
        "chemical": Theme(
            namespace="chemical",
            project_label="chemical weapons-project",
            batch_stem="compound_batch",
            aggregate_stem="stability_aggregate",
            gate_stem="isolation_gate",
            state_stem="agent_state",
            stage_stems=("chemical_assessment", "chemical_review"),
        ),
    }
)
