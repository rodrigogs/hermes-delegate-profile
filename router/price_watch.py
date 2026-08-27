"""Daily, isolated provenance watcher for supplier pricing text.

This module detects a change in the literal pricing clause that supports a time
window; it deliberately does not parse a price or mutate the capability
registry.  A false numeric extraction would be a routing decision disguised as
a daily maintenance task.  The caller injects network IO and Kanban creation so
unit tests remain offline and a failed supplier fetch can only preserve state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from difflib import unified_diff
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Sequence


Fetch = Callable[[str], str]
CreateCard = Callable[[Dict[str, str]], None]
Clock = Callable[[], datetime]


def _is_page_metadata(line: str) -> bool:
    """True when the matched line is the page shell (``<meta``/``<title``/``og:``).

    A line of the shell is not a pricing rule: the first real run "confirmed"
    xiaomi-token-plan while watching the ``og:title`` meta tag, so a sha256 that
    never changes was stored for a clause that could drift freely.  Anything the
    anchor resolves to must be a body clause or the anchor is refused.
    """
    lowered = line.casefold().lstrip()
    return lowered.startswith(("<meta", "<title")) or "og:" in lowered


@dataclass(frozen=True)
class ProviderAdapter:
    """Minimal supplier-specific knowledge needed to preserve a pricing clause."""

    provider: str
    url: str
    anchor: str
    key: str | None = None
    watchable: bool = True
    reason: str | None = None

    def extract(self, page: str) -> str:
        """Return the literal line containing the documented anchor.

        Supplier pages regularly change markup around their pricing note.  The
        surrounding HTML is intentionally discarded; the complete matching line
        is stable enough to diff while retaining the timezone words the supplier
        used.  An anchor whose first hit is the page shell (``<meta``, ``<title``,
        ``og:``) raises instead of confirming: a title is not a rule, and a
        "confirmed" that watches the title is exactly the false confidence this
        detector exists to kill.
        """
        for raw_line in page.splitlines():
            if self.anchor.casefold() in raw_line.casefold():
                line = " ".join(raw_line.strip().split())
                if _is_page_metadata(line):
                    raise ValueError(
                        f"pricing anchor resolved to page metadata, not a rule clause: {self.anchor}"
                    )
                return line
        raise ValueError(f"pricing anchor not found: {self.anchor}")


@dataclass(frozen=True)
class WatchResult:
    checked: List[str]
    confirmed: List[str]
    changed: List[str]
    failed: List[str]
    unwatchable: List[str] = field(default_factory=list)


def _empty_state() -> Dict[str, Any]:
    return {"providers": {}}


def _read_state(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return _empty_state()
    return raw


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _timestamp(now: Clock) -> str:
    return now().isoformat()


def _provider_registry(provider: str, registry: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        model: dict(entry)
        for model, entry in registry.items()
        if entry.get("provider") == provider or provider in model
    }


def _card(
    adapter: ProviderAdapter,
    previous: str,
    current: str,
    registry: Mapping[str, Mapping[str, Any]],
) -> Dict[str, str]:
    diff = "\n".join(
        unified_diff(
            [previous + "\n"],
            [current + "\n"],
            fromfile="provider text (previous)",
            tofile="provider text (current)",
        )
    )
    relevant_registry = _provider_registry(adapter.provider, registry)
    return {
        "title": f"Revisar mudança de preço: {adapter.provider}",
        "body": (
            f"A página do fornecedor mudou o bloco que sustenta uma janela de preço.\n\n"
            f"URL: {adapter.url}\n"
            f"Âncora: {adapter.anchor}\n\n"
            f"```diff\n{diff}\n```\n\n"
            f"Entrada atual do registry:\n```json\n"
            f"{json.dumps(relevant_registry, ensure_ascii=False, indent=2, sort_keys=True)}\n```\n\n"
            "Patch proposto: revisar a entrada acima contra o texto literal; não aplicar "
            "automaticamente."
        ),
    }


def _eligible(
    adapters: Iterable[ProviderAdapter],
    registered_providers: set[str],
    policy_providers: set[str],
) -> List[ProviderAdapter]:
    return [
        adapter
        for adapter in adapters
        if adapter.provider in registered_providers and adapter.provider in policy_providers
    ]


def run_watch(
    *,
    state_path: Path,
    adapters: Sequence[ProviderAdapter],
    registered_providers: set[str],
    policy_providers: set[str],
    registry: Mapping[str, Mapping[str, Any]],
    fetch: Fetch,
    create_card: CreateCard,
    now: Clock,
) -> WatchResult:
    """Check eligible providers and persist provenance without changing routing data.

    First observations establish a baseline.  Equal later observations refresh
    ``verified_at``; a changed clause creates a review card and advances the
    observed baseline only after that explicit review signal exists.  Fetch and
    extraction failures retain the last successful literal block and append a
    dated failure record instead of treating absence as a flat price.  A target
    declared unwatchable is reported as a permanent condition in its own bucket —
    never as a daily failure — so it cannot become noise nobody reads.
    """
    state = _read_state(state_path)
    providers = state["providers"]
    checked: List[str] = []
    confirmed: List[str] = []
    changed: List[str] = []
    failed: List[str] = []
    unwatchable: List[str] = []

    for adapter in _eligible(adapters, registered_providers, policy_providers):
        key = adapter.key or adapter.provider
        recorded = providers.get(key)
        if not adapter.watchable:
            # Declared unwatchable is a PERMANENT condition, not a daily failure:
            # a target that can never be fetched would otherwise fail every run
            # and become noise nobody reads.  Report it in its own bucket and
            # persist the declaration so the state says why, once.
            unwatchable.append(key)
            previous = recorded if isinstance(recorded, MutableMapping) else {}
            previous_permanent = previous.get("permanent")
            since = (
                previous_permanent["since"]
                if isinstance(previous_permanent, Mapping)
                else _timestamp(now)
            )
            providers[key] = {
                **{
                    k: v
                    for k, v in previous.items()
                    if k in ("url", "anchor", "literal", "sha256", "verified_at")
                },
                "url": adapter.url,
                "anchor": adapter.anchor,
                "reason": adapter.reason or "not watchable by simple fetch",
                "permanent": {"status": "unwatchable", "since": since},
            }
            continue
        checked.append(key)
        try:
            literal = adapter.extract(fetch(adapter.url))
        except (OSError, ValueError) as exc:
            failed.append(key)
            previous = recorded if isinstance(recorded, MutableMapping) else {}
            providers[key] = {
                **{k: v for k, v in previous.items() if k not in ("permanent", "reason")},
                "last_failure_at": _timestamp(now),
                "last_failure": str(exc),
            }
            continue

        observed = {
            "url": adapter.url,
            "anchor": adapter.anchor,
            "literal": literal,
            "sha256": sha256(literal.encode("utf-8")).hexdigest(),
            "verified_at": _timestamp(now),
        }
        if not isinstance(recorded, MutableMapping):
            providers[key] = observed
            continue
        if recorded.get("anchor") != adapter.anchor:
            # Re-anchor is a deliberate operator act; the old literal belongs to
            # a different clause and its diff against the new one is noise, not a
            # rule change.  Establish the new baseline silently.
            providers[key] = observed
            continue
        previous_literal = recorded.get("literal")
        if previous_literal == literal:
            confirmed.append(key)
            providers[key] = {
                **{k: v for k, v in recorded.items() if k not in ("permanent", "reason")},
                **observed,
            }
            continue
        if isinstance(previous_literal, str):
            create_card(_card(adapter, previous_literal, literal, registry))
            changed.append(key)
        providers[key] = observed

    _write_state(state_path, state)
    return WatchResult(
        checked=checked, confirmed=confirmed, changed=changed, failed=failed, unwatchable=unwatchable
    )
