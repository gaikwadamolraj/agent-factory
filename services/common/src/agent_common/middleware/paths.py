from dataclasses import dataclass, field


@dataclass(frozen=True)
class PathRules:
    """Which paths a middleware applies to: `protected` prefixes, minus exact `public` paths and `public_prefixes`."""

    protected: tuple[str, ...] = ("/",)
    public: frozenset[str] = field(default_factory=frozenset)
    public_prefixes: tuple[str, ...] = ()

    def applies(self, path: str) -> bool:
        if path in self.public or any(path.startswith(p) for p in self.public_prefixes):
            return False
        return any(path.startswith(p) for p in self.protected)


def header(scope, name: str) -> str:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return ""
