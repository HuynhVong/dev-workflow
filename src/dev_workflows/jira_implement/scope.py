"""Hard repo allow-list. Built once in preflight; only an explicit human decision widens it."""
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class ScopeViolation(PermissionError):
    pass


@dataclass(frozen=True)
class ScopeGuard:
    repos: Mapping[str, str]  # name -> absolute path

    @classmethod
    def create(cls, repos: dict[str, str]) -> "ScopeGuard":
        return cls(MappingProxyType({n: os.path.realpath(p) for n, p in repos.items()}))

    @classmethod
    def from_state(cls, scope: dict[str, str]) -> "ScopeGuard":
        return cls.create(scope)

    def to_state(self) -> dict[str, str]:
        return dict(self.repos)

    def require_repo(self, name: str) -> str:
        if name not in self.repos:
            raise ScopeViolation(f"repo '{name}' is outside the allowed scope {sorted(self.repos)}")
        return self.repos[name]

    def contains_path(self, path: str) -> bool:
        real = os.path.realpath(path)
        return any(real == root or real.startswith(root + os.sep) for root in self.repos.values())

    def require_path(self, path: str) -> str:
        if not self.contains_path(path):
            raise ScopeViolation(f"path '{path}' is outside the allowed repos")
        return os.path.realpath(path)

    def widened(self, name: str, path: str) -> "ScopeGuard":
        """Only called after a logged human approval of a scope request."""
        return ScopeGuard.create({**self.repos, name: path})
