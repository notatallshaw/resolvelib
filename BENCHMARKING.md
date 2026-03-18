# Benchmarking `_build_result`

This branch adds a `disconnected` set to `_has_route_to_root` for
cycle detection and negative caching. The benchmark measures whether
this has meaningful performance impact on `_build_result`.

## Running against resolvelib fixtures

```bash
# From the resolvelib repo root, using the nox test environment:
.nox/tests-3-13/bin/python bench_build_result.py

# Or after `pip install -e .[test]`:
python bench_build_result.py
```

This resolves all functional test cases (Python/PyPI fixtures), picks
the largest resolved state, and times `_build_result` 1000 times for
both the original and new implementations.

Resolvelib's test fixtures are small (max ~20 resolved packages), so
this is a lower bound on real-world impact.

## Adapting for pip

The benchmark matters most at pip's scale (tens to hundreds of resolved
packages). To test with pip's resolver output:

1. In pip's resolver, after `resolution.resolve()` returns the final
   `State`, pickle it:

   ```python
   import pickle
   # In pip._internal.resolution.resolvelib.resolver, after resolve()
   state = resolution.resolve(requirements, max_rounds=...)
   with open("/tmp/pip_state.pkl", "wb") as f:
       pickle.dump(state, f)
   ```

2. Modify `bench_build_result.py` to load the pickled state instead of
   resolving from fixtures:

   ```python
   import pickle
   with open("/tmp/pip_state.pkl", "rb") as f:
       state = pickle.load(f)
   bench_build_result(state)
   ```

   Or run a pip install that produces a large resolution (many
   transitive deps) and capture the state from there.

The key variable is the number of criteria in the final state. At 20
criteria (resolvelib fixtures), there is no measurable difference. The
question is whether pip's typical resolutions (50-200+ criteria) show
a difference.

## What the benchmark compares

- **original**: `_has_route_to_root` with only a `connected` set
  (positive cache). No cycle detection. Each unreachable node is
  re-explored on every top-level call from `_build_result`.

- **disconnected**: Adds a `disconnected` set shared across all calls.
  Nodes are assumed disconnected before exploration; moved to
  `connected` if a root path is found. Provides both cycle detection
  and negative caching (unreachable nodes are never re-explored).
