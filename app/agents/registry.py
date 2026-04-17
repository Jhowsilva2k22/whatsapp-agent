"""
EcoZap — Agent Registry
=======================
Registro central de todos os agentes do sistema.
Adicionar novo agente = importar aqui + chamar register().
"""
from typing import Type, Optional
from app.agents.base import Agent, AuthorityLevel
import logging

logger = logging.getLogger(__name__)

# Registro global: role → classe do agente
_REGISTRY: dict[str, Type[Agent]] = {}
# Instâncias singleton: role → instância
_INSTANCES: dict[str, Agent] = {}


def register(agent_class: Type[Agent]) -> Type[Agent]:
    """
    Decorator para registrar um agente.
    Uso:
        @register
        class Sentinel(Agent):
            role = "sentinel"
    """
    role = agent_class.role
    if role in _REGISTRY:
        logger.warning(f"[Registry] Agente '{role}' já registrado. Sobrescrevendo.")
    _REGISTRY[role] = agent_class
    logger.info(f"[Registry] Agente registrado: {role} ({agent_class.display_name})")
    return agent_class


def get_agent(role: str) -> Optional[Agent]:
    """Retorna instância singleton do agente pelo role."""
    if role not in _INSTANCES:
        if role not in _REGISTRY:
            logger.error(f"[Registry] Agente '{role}' não encontrado. Registrados: {list(_REGISTRY.keys())}")
            return None
        _INSTANCES[role] = _REGISTRY[role]()
        logger.info(f"[Registry] Instância criada para: {role}")
    return _INSTANCES[role]


def get_all_agents() -> list[Agent]:
    """Retorna todas as instâncias de agentes registrados."""
    return [get_agent(role) for role in _REGISTRY]


def get_agents_by_department(department: str) -> list[Agent]:
    """Retorna agentes de um departamento específico."""
    return [
        get_agent(role)
        for role, cls in _REGISTRY.items()
        if cls.department == department
    ]


def get_agents_by_authority(max_level: AuthorityLevel) -> list[Agent]:
    """Retorna agentes com autoridade até o nível especificado."""
    return [
        get_agent(role)
        for role, cls in _REGISTRY.items()
        if cls.authority_level <= max_level
    ]


def list_registered() -> list[dict]:
    """Lista todos os agentes registrados com seus metadados."""
    return [
        {
            "role": cls.role,
            "display_name": cls.display_name,
            "department": cls.department,
            "authority_level": cls.authority_level.name,
            "opinion_bias": cls.opinion_bias,
        }
        for cls in _REGISTRY.values()
    ]


# ─── Auto-import de agentes registrados ──────────────────────────────────────
# Adicione aqui cada novo agente para que seja carregado automaticamente.

def load_all_agents():
    """
    Importa todos os módulos de agentes para que os @register decorators
    sejam executados. Chamado na inicialização da aplicação.
    """
    # Equipe de Infra (OPS)
    try:
        from app.agents.ops import sentinel   # noqa: F401
        from app.agents.ops import doctor     # noqa: F401
        from app.agents.ops import surgeon    # noqa: F401
        from app.agents.ops import guardian   # noqa: F401
        logger.info("[Registry] Equipe OPS carregada")
    except ImportError as e:
        logger.warning(f"[Registry] OPS parcialmente carregado: {e}")

    # Equipe de Negócio (BUSINESS)
    try:
        from app.agents.business import attendant   # noqa: F401
        from app.agents.business import sdr         # noqa: F401
        from app.agents.business import closer      # noqa: F401
        from app.agents.business import consultant  # noqa: F401
        logger.info("[Registry] Equipe COMMERCIAL carregada (Attendant + SDR + Closer + Consultant)")
    except ImportError as e:
        logger.warning(f"[Registry] Business parcialmente carregado: {e}")

    logger.info(f"[Registry] Total de agentes registrados: {len(_REGISTRY)}")
    for info in list_registered():
        logger.info(f"  → {info['role']:20} | {info['department']:12} | {info['authority_level']}")
