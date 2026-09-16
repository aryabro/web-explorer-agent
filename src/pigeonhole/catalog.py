from __future__ import annotations

from pathlib import Path
from typing import Any

from pigeonhole.contracts import Capability
from pigeonhole.replay import load_capability


def capability_dir(path: str | Path = "capabilities") -> Path:
    return Path(path)


def iter_capabilities(directory: str | Path = "capabilities") -> list[tuple[Path, Capability]]:
    root = capability_dir(directory)
    found: list[tuple[Path, Capability]] = []
    if not root.exists():
        return found
    for path in sorted(root.glob("*.json")):
        found.append((path, load_capability(path)))
    return found


def find_capability(capability_id: str, directory: str | Path = "capabilities") -> Path:
    for path, capability in iter_capabilities(directory):
        if capability.contract.id == capability_id or path.stem == capability_id:
            return path
    raise FileNotFoundError(f"no capability named {capability_id}")


def summarize(capability: Capability, path: Path) -> dict[str, Any]:
    return {
        "id": capability.contract.id,
        "version": capability.contract.version,
        "title": capability.contract.title,
        "path": path.as_posix(),
        "approval": capability.governance.approval.status,
        "tenant": capability.compatibility.tenant,
        "inputs": list(capability.contract.inputs),
        "outputs": list(capability.contract.outputs),
        "outcomes": [item.code for item in capability.contract.business_outcomes],
    }


def tool_definitions(directory: str | Path = "capabilities") -> list[dict[str, Any]]:
    tools = []
    for _, capability in iter_capabilities(directory):
        properties = {
            name: {
                "type": parameter.type,
                "description": parameter.description,
            }
            for name, parameter in capability.contract.inputs.items()
        }
        required = [
            name
            for name, parameter in capability.contract.inputs.items()
            if parameter.required and name not in {"operator_id", "pin"}
        ]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": capability.contract.id.replace(".", "_"),
                    "description": capability.contract.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return tools
