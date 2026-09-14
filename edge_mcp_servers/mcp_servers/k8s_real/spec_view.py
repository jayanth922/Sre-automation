#!/usr/bin/env python3
"""Rendering a container's declared configuration for an investigating agent.

Split out of ``server.py`` (and dependency-free, like the Executor server's
``guardrails.py``) because this is the point where cluster configuration
becomes LLM prompt text and, from there, a Slack message. What it redacts is a
security boundary, so it is unit-tested directly rather than only through a
live cluster.
"""

import re
from typing import Any, Dict, List, Optional

# Env var names whose *value* must never leave the cluster. The investigating
# agent sends whatever a tool returns to an LLM and, from there, into the Slack
# war room — so a deployment read is a data-exfiltration path unless the values
# a workload keeps inline (an API key someone pasted into the Deployment rather
# than referencing a Secret) are stripped here, at the source.
SECRETISH_ENV_NAME = re.compile(
    r"SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|APIKEY|API_KEY|ACCESS_KEY|"
    r"SESSION_KEY|SALT|SIGNING|_DSN$|_AUTH$|_KEY$",
    re.IGNORECASE,
)

REDACTED = "<redacted>"


def value_source(var: Any) -> Optional[str]:
    """Where an env var without a literal value comes from, as a short label.

    Reported rather than resolved: naming the Secret is diagnostic ("this is
    wired to the wrong secret"), reading it would not be.
    """
    value_from = getattr(var, "value_from", None)
    if not value_from:
        return None
    secret_ref = getattr(value_from, "secret_key_ref", None)
    if secret_ref:
        return f"secret:{secret_ref.name}/{secret_ref.key}"
    config_ref = getattr(value_from, "config_map_key_ref", None)
    if config_ref:
        return f"configMap:{config_ref.name}/{config_ref.key}"
    field_ref = getattr(value_from, "field_ref", None)
    if field_ref:
        return f"field:{field_ref.field_path}"
    resource_ref = getattr(value_from, "resource_field_ref", None)
    if resource_ref:
        return f"resource:{resource_ref.resource}"
    return "unknown"


def format_container_spec(spec: Any) -> Dict[str, Any]:
    """One container's declared configuration: image, env, resources.

    This is the half of a Deployment that ``list_deployments`` and
    ``get_deployment_status`` both omit — they report replica arithmetic, which
    answers "is it running" but never "what is it running". A misconfigured env
    var or a bad image tag is invisible to an investigation that can only see
    the former.
    """
    env: Dict[str, Optional[str]] = {}
    env_sources: Dict[str, str] = {}
    redacted: List[str] = []
    for var in (getattr(spec, "env", None) or []):
        source = value_source(var)
        if source:
            env_sources[var.name] = source
        value = getattr(var, "value", None)
        if value is None:
            # Distinguishable from "absent": the name is present with a null
            # value, and env_sources says where the value comes from.
            env[var.name] = None
            continue
        if SECRETISH_ENV_NAME.search(var.name):
            env[var.name] = REDACTED
            redacted.append(var.name)
        else:
            env[var.name] = value

    resources = getattr(spec, "resources", None)
    entry: Dict[str, Any] = {
        "container": spec.name,
        "image": getattr(spec, "image", None),
        "image_pull_policy": getattr(spec, "image_pull_policy", None),
        "env": env,
        "limits": dict(getattr(resources, "limits", None) or {}) if resources else {},
        "requests": dict(getattr(resources, "requests", None) or {}) if resources else {},
    }
    if env_sources:
        entry["env_sources"] = env_sources
    if redacted:
        entry["env_redacted"] = redacted

    # envFrom pulls in a whole ConfigMap/Secret; none of its keys appear in
    # `env`, so a spec that looks bare here may not be. Say so rather than let
    # the agent conclude the container has no configuration.
    env_from: List[str] = []
    for ref in (getattr(spec, "env_from", None) or []):
        config_ref = getattr(ref, "config_map_ref", None)
        secret_ref = getattr(ref, "secret_ref", None)
        if config_ref:
            env_from.append(f"configMap:{config_ref.name}")
        elif secret_ref:
            env_from.append(f"secret:{secret_ref.name}")
    if env_from:
        entry["env_from"] = env_from
    return entry
