#!/usr/bin/env python3
"""
Policy Engine for SRE Agent - Deterministic Safety Rules

Implements safety checks for remediation actions to prevent dangerous
operations in production environments.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .agent_state import RemediationAction

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def evaluate_action(
    action: RemediationAction, environment: str, risk_score: float = 0.0
) -> tuple[bool, str]:
    """
    Evaluate if a remediation action is allowed based on deterministic rules.

    Rules:
    - Block RESTART on PROD unless risk_score < 3
    - Block DELETE on PROD
    - Block SCALE DOWN to 0 on PROD
    - Allow all actions in non-PROD environments

    Args:
        action: The remediation action to evaluate
        environment: Environment name (e.g., "production", "staging", "dev")
        risk_score: Risk score from remediation plan (0-10 scale, lower is safer)

    Returns:
        Tuple of (is_allowed: bool, reason: str)
    """
    env_lower = environment.lower()
    action_type = action.action_type.lower()
    target = action.target.lower()

    logger.info(
        f"🔒 PolicyEngine: Evaluating action '{action_type}' on '{target}' in '{environment}' (risk: {risk_score})"
    )

    # Rule 1: Block RESTART on PROD unless risk < threshold (configurable)
    restart_risk_threshold = float(os.getenv("POLICY_RESTART_RISK_THRESHOLD", "3.0"))
    if action_type == "restart" and env_lower == "production":
        if risk_score >= restart_risk_threshold:
            reason = f"RESTART blocked on PROD: Risk score {risk_score} >= {restart_risk_threshold}"
            logger.warning(f"🚫 PolicyEngine: {reason}")
            return False, reason
        else:
            reason = f"RESTART allowed on PROD: Risk score {risk_score} < {restart_risk_threshold}"
            logger.info(f"✅ PolicyEngine: {reason}")
            return True, reason

    # Rule 2: Block DELETE on PROD
    if action_type in ["delete", "patch"] and "delete" in action_type and env_lower == "production":
        reason = f"DELETE blocked on PROD: Too dangerous for production"
        logger.warning(f"🚫 PolicyEngine: {reason}")
        return False, reason

    # Rule 3: Block SCALE DOWN to 0 on PROD
    if action_type == "scale":
        scale_params = action.parameters
        if isinstance(scale_params, dict):
            replicas = scale_params.get("replicas", scale_params.get("replica_count", None))
            if replicas == 0 and env_lower == "production":
                reason = f"SCALE DOWN to 0 blocked on PROD: Would cause service outage"
                logger.warning(f"🚫 PolicyEngine: {reason}")
                return False, reason

    # Rule 4: Block ROLLBACK on PROD without explicit approval flag
    if action_type == "rollback" and env_lower == "production":
        # Check if action has explicit approval flag in parameters
        if isinstance(action.parameters, dict):
            explicit_approval = action.parameters.get("explicit_approval", False)
            if not explicit_approval:
                reason = f"ROLLBACK blocked on PROD: Requires explicit approval flag"
                logger.warning(f"🚫 PolicyEngine: {reason}")
                return False, reason

    # Rule 5: Allow all actions in non-PROD environments
    if env_lower != "production":
        reason = f"Action allowed: Non-production environment ({environment})"
        logger.info(f"✅ PolicyEngine: {reason}")
        return True, reason

    # Rule 6: Default allow for other action types in PROD (with caution)
    reason = f"Action allowed: {action_type} in {environment} (default policy)"
    logger.info(f"✅ PolicyEngine: {reason}")
    return True, reason


def get_environment_from_context(alert_context) -> str:
    """
    Extract environment from alert context.

    Args:
        alert_context: AlertContext object or dict

    Returns:
        Environment string (defaults to "production" if not found)
    """
    if alert_context is None:
        return "production"  # Default to production for safety

    if hasattr(alert_context, "labels"):
        labels = alert_context.labels
    elif isinstance(alert_context, dict):
        labels = alert_context.get("labels", {})
    else:
        return "production"

    # Check common label names for environment
    env = (
        labels.get("environment")
        or labels.get("env")
        or labels.get("namespace")  # Sometimes namespace indicates env
        or "production"  # Default to production for safety
    )

    return env.lower()


def calculate_risk_score(remediation_plan) -> float:
    """
    Calculate risk score from remediation plan.

    Args:
        remediation_plan: RemediationPlan object

    Returns:
        Risk score (0-10, where 0 is safest, 10 is most dangerous)
    """
    if remediation_plan is None:
        return 5.0  # Default medium risk

    # Map risk_level to numeric score
    risk_level_map = {
        "low": 2.0,
        "medium": 5.0,
        "high": 8.0,
    }

    base_score = risk_level_map.get(remediation_plan.risk_level, 5.0)

    # Adjust based on number of actions (more actions = higher risk)
    action_count = len(remediation_plan.actions)
    if action_count > 3:
        base_score += 1.0
    if action_count > 5:
        base_score += 1.0

    # Adjust based on action types
    dangerous_actions = ["restart", "rollback", "delete"]
    for action in remediation_plan.actions:
        if action.action_type.lower() in dangerous_actions:
            base_score += 0.5

    # Cap at 10.0
    return min(base_score, 10.0)
