from enum import Enum

from app.config import settings


class ModelRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    RERANKER = "reranker"
    GENERATOR = "generator"


_MODEL_MAP = {
    ModelRole.ORCHESTRATOR: settings.orchestrator_model,
    ModelRole.RERANKER: settings.reranker_model,
    ModelRole.GENERATOR: settings.generator_model,
}


def get_model(role: ModelRole) -> str:
    return _MODEL_MAP[role]
