"""``define_node``: declare a node's contract and bind its handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .config import EmptyConfig, NodeConfig
from .context import NodeContext, NodeInputs, NodeResult
from .manifest import (
    Compatibility,
    ExecutionPolicy,
    InputPort,
    Mode,
    NodeManifest,
    OutputPort,
    Permissions,
)

C = TypeVar("C", bound=NodeConfig)
Handler = Callable[[NodeContext, NodeInputs, C], Awaitable[NodeResult]]


@dataclass(frozen=True)
class NodeDefinition(Generic[C]):
    manifest: NodeManifest
    config_model: type[C]
    handler: Handler[C]

    @property
    def key(self) -> tuple[str, str]:
        return (self.manifest.type, self.manifest.version)

    def parse_config(self, raw: Mapping[str, Any]) -> C:
        return self.config_model.model_validate(dict(raw))


def define_node(
    *,
    type: str,
    version: str,
    display_name: str,
    category: str,
    package: str,
    description: str = "",
    inputs: Iterable[InputPort] = (),
    outputs: Iterable[OutputPort] = (),
    branches: Iterable[str] | None = None,
    config: type[C] = EmptyConfig,  # type: ignore[assignment]
    permissions: Permissions | None = None,
    execution: ExecutionPolicy | None = None,
    compatibility: Compatibility | None = None,
    modes: Iterable[Mode] | None = None,
    tags: Iterable[str] = (),
    branches_from_config: str | None = None,
    control_join: str = "any",
    runtime_role: str | None = None,
) -> Callable[[Handler[C]], NodeDefinition[C]]:
    """Decorator turning an async handler into a registered-ready ``NodeDefinition``.

    The manifest is validated eagerly, so contract mistakes fail at import time, not at run time.
    """

    def decorator(handler: Handler[C]) -> NodeDefinition[C]:
        manifest_kwargs: dict[str, Any] = dict(
            type=type,
            version=version,
            display_name=display_name,
            category=category,
            package=package,
            description=description or (handler.__doc__ or "").strip(),
            inputs=list(inputs),
            outputs=list(outputs),
            config_schema=config.model_json_schema(by_alias=True),
            permissions=permissions or Permissions(),
            execution=execution or ExecutionPolicy(),
            compatibility=compatibility or Compatibility(),
            tags=list(tags),
            branches_from_config=branches_from_config,
            control_join=control_join,
            runtime_role=runtime_role,
        )
        if branches is not None:
            manifest_kwargs["branches"] = list(branches)
        if modes is not None:
            manifest_kwargs["modes"] = list(modes)
        return NodeDefinition(manifest=NodeManifest(**manifest_kwargs), config_model=config, handler=handler)

    return decorator
