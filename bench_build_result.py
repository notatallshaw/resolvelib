"""Benchmark _build_result with and without the disconnected set.

Resolves all resolvelib functional test cases, then benchmarks
_build_result on the largest resolved state. The two implementations
compared:

  original:     No disconnected set, no cycle detection.
  disconnected: Adds a disconnected set for negative caching and
                cycle detection in _has_route_to_root.

To adapt for pip: replace the resolution step with pip's resolver to
produce a State with more packages. See BENCHMARKING.md for details.
"""

import collections
import json
import operator
import os
import time
from functools import reduce

import packaging.markers
import packaging.requirements
import packaging.specifiers
import packaging.utils
import packaging.version

from resolvelib import AbstractProvider, Resolver
from resolvelib.reporters import BaseReporter
from resolvelib.resolvers.abstract import Result
from resolvelib.resolvers.resolution import Resolution, _build_result
from resolvelib.structs import DirectedGraph

Candidate = collections.namedtuple("Candidate", "name version extras")


def _eval_marker(marker, extras=(None,)):
    if not marker:
        return True
    if not isinstance(marker, packaging.markers.Marker):
        marker = packaging.markers.Marker(marker)
    return any(marker.evaluate({"extra": extra}) for extra in extras)


class BenchProvider(AbstractProvider):
    """Provider that reads from resolvelib's functional test fixtures."""

    def __init__(self, filename):
        with open(filename) as f:
            case_data = json.load(f)
        index_name = os.path.normpath(
            os.path.join(filename, "..", "..", "index", case_data["index"] + ".json"),
        )
        with open(index_name) as f:
            self.index = json.load(f)
        self.root_requirements = [
            packaging.requirements.Requirement(r) for r in case_data["requested"]
        ]

    def identify(self, requirement_or_candidate):
        name = packaging.utils.canonicalize_name(requirement_or_candidate.name)
        if requirement_or_candidate.extras:
            extras_str = ",".join(sorted(requirement_or_candidate.extras))
            return f"{name}[{extras_str}]"
        return name

    def get_preference(self, identifier, resolutions, candidates, information, backtrack_causes):
        transitive = all(p is not None for _, p in information[identifier])
        return (transitive, identifier)

    def _iter_matches(self, identifier, requirements, incompatibilities):
        name, _, _ = identifier.partition("[")
        bad_versions = {c.version for c in incompatibilities[identifier]}
        extras = {e for r in requirements[identifier] for e in r.extras}
        available_versions = (
            v
            for key in self.index[name]
            if (v := packaging.version.parse(key)) not in bad_versions
        )
        combined_specifier = reduce(
            operator.and_,
            map(operator.attrgetter("specifier"), requirements[identifier]),
            packaging.specifiers.SpecifierSet(),
        )
        for v in combined_specifier.filter(available_versions):
            yield Candidate(name=name, version=v, extras=extras)

    def find_matches(self, identifier, requirements, incompatibilities):
        return sorted(
            self._iter_matches(identifier, requirements, incompatibilities),
            key=operator.attrgetter("version"),
            reverse=True,
        )

    def is_satisfied_by(self, requirement, candidate):
        return candidate.version in requirement.specifier

    def get_dependencies(self, candidate):
        name = packaging.utils.canonicalize_name(candidate.name)
        deps = []
        if candidate.extras:
            deps.append(packaging.requirements.Requirement(f"{name}=={candidate.version}"))
        for r in self.index[name][str(candidate.version)]["dependencies"]:
            requirement = packaging.requirements.Requirement(r)
            if not _eval_marker(requirement.marker, candidate.extras):
                continue
            deps.append(requirement)
        return deps


# -- Original implementation (baseline) ---------------------------------------

def _has_route_to_root_original(criteria, key, all_keys, connected):
    if key in connected:
        return True
    if key not in criteria:
        return False
    assert key is not None
    for p in criteria[key].iter_parent():
        try:
            pkey = all_keys[id(p)]
        except KeyError:
            continue
        if pkey in connected:
            connected.add(key)
            return True
        if _has_route_to_root_original(criteria, pkey, all_keys, connected):
            connected.add(key)
            return True
    return False


def _build_result_original(state):
    mapping = state.mapping
    all_keys = {id(v): k for k, v in mapping.items()}
    all_keys[id(None)] = None

    graph = DirectedGraph()
    graph.add(None)

    connected = {None}
    for key, criterion in state.criteria.items():
        if not _has_route_to_root_original(state.criteria, key, all_keys, connected):
            continue
        if key not in graph:
            graph.add(key)
        for p in criterion.iter_parent():
            try:
                pkey = all_keys[id(p)]
            except KeyError:
                continue
            if pkey not in graph:
                graph.add(pkey)
            graph.connect(pkey, key)

    return Result(
        mapping={k: v for k, v in mapping.items() if k in connected},
        graph=graph,
        criteria=state.criteria,
    )


# -- Benchmark runner ---------------------------------------------------------

def resolve_all_cases():
    """Resolve all functional test cases, return the largest state."""
    cases_dir = os.path.join(
        os.path.dirname(__file__), "tests", "functional", "python", "inputs", "case"
    )
    best_state = None
    best_name = None
    best_size = 0

    for name in sorted(os.listdir(cases_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cases_dir, name)
        try:
            provider = BenchProvider(path)
            resolution = Resolution(provider, BaseReporter())
            state = resolution.resolve(provider.root_requirements, max_rounds=600)
            size = len(state.criteria)
            print(f"  {name}: {len(state.mapping)} mapped, {size} criteria")
            if size > best_size:
                best_size = size
                best_state = state
                best_name = name
        except Exception as e:
            print(f"  {name}: {type(e).__name__}")

    return best_name, best_state


def bench_build_result(state, n=1000):
    """Time both implementations of _build_result."""
    # Warmup
    _build_result_original(state)
    _build_result(state)

    start = time.perf_counter()
    for _ in range(n):
        _build_result_original(state)
    elapsed_original = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(n):
        _build_result(state)
    elapsed_new = time.perf_counter() - start

    return elapsed_original, elapsed_new


if __name__ == "__main__":
    name, state = resolve_all_cases()
    print(f"\nBenchmarking: {name} ({len(state.criteria)} criteria)")

    N = 1000
    elapsed_original, elapsed_new = bench_build_result(state, N)

    print(f"\n_build_result x{N}:")
    print(f"  original:      {elapsed_original:.4f}s  ({elapsed_original / N * 1e6:.1f} us/call)")
    print(f"  disconnected:  {elapsed_new:.4f}s  ({elapsed_new / N * 1e6:.1f} us/call)")
    print(f"  speedup:       {elapsed_original / elapsed_new:.2f}x")
